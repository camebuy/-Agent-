"""招聘业务工具：岗位记录、投递状态、逐岗位审批和定时授权范围。"""

import json
import time
import os
import uuid
import hashlib
import re
import threading
from string import Template
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from src.tools.activity_tool import (
    SCHEDULE_LOCK,
    _load_schedules,
    add_schedule,
    get_schedule_record,
    read_cards,
    send_user_request,
    update_schedule,
    user_calendar_conflicts,
)
from src.core.system.runtime_settings import load_runtime_settings
from src.tools.database_tool import get_file_path, get_task_resources, read_user_file
from src.tools.browser_atomic_tools import (
    _SESSION_LOCK,
    _evaluate_json,
    _js_literal,
    _page_info,
    _web_click_unchecked,
    _web_upload_unchecked,
    web_input,
)
from src.core.system.path_manager import PathManager
from src.core.tasks.schedule_runner import get_schedule_lineage
from src.core.tasks.task_context import get_current_task

_JOBS_LOCK = threading.RLock()
_JOB_METADATA_FIELDS = (
    "title", "company", "salary", "location", "publish_date", "education", "experience",
    "url", "job_url", "jd_text",
)


def _require_local_resume_permission(resume_delivery_mode: str) -> None:
    """An online profile does not need access to the user's local files."""
    if str(resume_delivery_mode or "file_upload").strip() != "file_upload":
        return
    from src.core.system.runtime_settings import load_runtime_settings
    if not load_runtime_settings()["tool_permissions"].get("user_files", True):
        raise PermissionError("用户文件工具已停用，不能读取或上传本地简历。")


def _job_metadata(job_info: Dict[str, Any]) -> Dict[str, str]:
    """Keep untrusted page extraction separate from application and review state."""
    if not isinstance(job_info, dict):
        raise ValueError("岗位信息必须是字典。")
    metadata = {}
    for key in _JOB_METADATA_FIELDS:
        value = job_info.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = value.strip()
    return metadata


class SubmissionNotConfirmedError(RuntimeError):
    """The platform outcome is absent or ambiguous; never retry blindly."""


class JobRecordManager:
    """招聘巡航岗位记录管理器"""

    def __init__(self):
        self.storage_file = PathManager.RECRUITMENT_FILE
        self._ensure_storage()

    def _ensure_storage(self):
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)

    def _read(self):
        if not self.storage_file.exists():
            return {"jobs": {}, "updated_at": time.time()}
        with self.storage_file.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
            raise ValueError("岗位记录文件格式损坏，原文件已保留。")
        return data

    def _save(self, data):
        temp = self.storage_file.with_name(self.storage_file.name + f".{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.storage_file)
        finally:
            temp.unlink(missing_ok=True)

    def is_recorded(self, job_id: str) -> bool:
        with _JOBS_LOCK:
            return job_id in self._read()["jobs"]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with _JOBS_LOCK:
            job = self._read()["jobs"].get(str(job_id))
            return deepcopy(job) if job is not None else None

    def record_job(self, job_id: str, job_info: Dict[str, Any], status: str = "PENDING"):
        with _JOBS_LOCK:
            data = self._read()
            data["jobs"][str(job_id)] = {
                "job_id": str(job_id), **_job_metadata(job_info), "status": status,
            }
            data["updated_at"] = time.time()
            self._save(data)

    def fill_missing_job_fields(self, job_id: str, observed: Dict[str, Any]) -> bool:
        """Add later-observed metadata without changing approval or application state."""
        metadata = _job_metadata(observed)
        with _JOBS_LOCK:
            data = self._read()
            job = data["jobs"].get(str(job_id))
            if not isinstance(job, dict):
                return False
            changed = False
            if not (job.get("url") or job.get("job_url")):
                for key in ("url", "job_url"):
                    value = metadata.get(key, "")
                    if value and urlsplit(value).scheme.lower() in {"http", "https"} and urlsplit(value).hostname:
                        job[key] = value
                        changed = True
                        break
            for key in ("title", "company", "salary", "location", "publish_date", "education", "experience", "jd_text"):
                if not job.get(key) and metadata.get(key):
                    job[key] = metadata[key]
                    changed = True
            if changed:
                data["updated_at"] = time.time()
                self._save(data)
            return changed

    def get_all_jobs(self) -> Dict[str, Any]:
        with _JOBS_LOCK:
            return deepcopy(self._read()["jobs"])

    def set_application(self, job_id: str, application: Dict[str, Any],
                        *, expected_application_id: Optional[str] = None,
                        expected_statuses: Optional[set[str]] = None) -> Dict[str, Any]:
        """Atomically replace one application's state without touching job metadata."""
        with _JOBS_LOCK:
            data = self._read()
            job = data["jobs"].get(str(job_id))
            if not isinstance(job, dict):
                raise ValueError(f"岗位 {job_id} 尚未登记。")
            previous = job.get("application") or {}
            if expected_application_id is not None and previous.get("application_id") != expected_application_id:
                raise ValueError("投递记录已经变化，请重新读取状态。")
            if expected_statuses is not None and previous.get("status") not in expected_statuses:
                raise ValueError(f"当前投递状态为 {previous.get('status') or '未准备'}，不能重复提交。")
            job["application"] = deepcopy(application)
            job["status"] = application["status"]
            data["updated_at"] = time.time()
            self._save(data)
            return deepcopy(application)

    def update_job_status(self, job_id: str, status: str, expected_status: str = None) -> bool:
        with _JOBS_LOCK:
            data = self._read()
            if job_id not in data.get("jobs", {}):
                return False
            if expected_status and data["jobs"][job_id].get("status") != expected_status:
                return False
            job = data["jobs"][job_id]
            # A legacy review decision is separate from the exact application.
            # Its late callback must not overwrite an application's durable state.
            job["review_status"] = status
            if not job.get("application"):
                job["status"] = status
            data["updated_at"] = time.time()
            self._save(data)
            return True


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _job_url(job_id: str, job: Dict[str, Any]) -> str:
    url = str(job.get("url") or job.get("job_url") or "").strip()
    if not url and urlsplit(str(job_id)).scheme.lower() in {"http", "https"}:
        url = str(job_id).strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("岗位必须包含实际可访问的完整 http(s) 岗位网址；仅凭岗位编号不能投递。")
    return url


_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "_ga", "_gl",
    "mc_cid", "mc_eid", "ref", "referrer", "source", "src", "spm", "trk",
    "tracking", "track", "tracking_id", "trackingid", "ref_id", "refid",
    "from", "via", "origin", "campaign", "campaign_id", "campaignid",
    "ad_id", "adid", "li_fat_id",
}
_JOB_ID_QUERY_KEYS = {"jobid", "job_id", "job-id", "jid", "jobkey", "job_key", "jk", "gh_jid", "id"}


def _stable_query(query: str) -> list[tuple[str, str]]:
    return sorted(
        (key, value) for key, value in parse_qsl(query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    )


def _url_host_key(url: str) -> str:
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        return ""
    if port and port != (443 if parts.scheme.lower() == "https" else 80):
        host += f":{port}"
    return host


def _job_identity(url: str) -> Optional[str]:
    """Return a proven stable job key; ambiguous query strings fail closed."""
    parts = urlsplit(str(url or ""))
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return None
    host = _url_host_key(url)
    if not host:
        return None
    path = parts.path.rstrip("/") or "/"
    if host == "zhipin.com" or host.endswith(".zhipin.com"):
        match = re.fullmatch(r"/job_detail/([A-Za-z0-9_-]+)\.html", path)
        return "boss:" + match.group(1) if match else None
    query = _stable_query(parts.query)
    if query:
        if any(key.casefold() not in _JOB_ID_QUERY_KEYS or not value for key, value in query):
            return None
        # A query-based job page needs one unambiguous stable identifier.
        if len({value for _, value in query}) != 1:
            return None
        if path.rsplit("/", 1)[-1].casefold() in {"search", "list", "job-search"}:
            return None
        return f"query:{host}:{path}:{query[0][1]}"
    terminal = path.rsplit("/", 1)[-1].casefold()
    if terminal in {
        "job", "jobs", "job-search", "search", "list", "view", "detail", "apply",
        "position", "positions", "career", "careers", "vacancy", "vacancies",
    } or len(terminal) < 3:
        return None
    return f"path:{host}:{path}" if path != "/" else None


def _same_url(left: str, right: str) -> bool:
    """Compare observed and approved job identity while retaining the full approval URL."""
    identity = _job_identity(left)
    return bool(identity and identity == _job_identity(right))


def _site_scope_host(url: str) -> str:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _conflicting_job_with_attempt(job_id: str, job_url: str, all_jobs: dict) -> Optional[str]:
    identity = _job_identity(job_url)
    if not identity:
        raise ValueError("岗位 URL 缺少可确认的稳定岗位身份；请核对页面后再投递。")
    for other_id, other in all_jobs.items():
        if str(other_id) == job_id or not isinstance(other, dict):
            continue
        other_url = str(other.get("url") or other.get("job_url")
                        or (other.get("application") or {}).get("job_url") or "").strip()
        if not other_url and urlsplit(str(other_id)).scheme.lower() in {"http", "https"}:
            other_url = str(other_id)
        other_status = (other.get("application") or {}).get("status") or other.get("status")
        if other_status not in {"SUBMITTED", "APPLIED", "ATTEMPTING", "UNKNOWN", "NOT_SUBMITTED"}:
            continue
        if not other_url:
            # An older attempted record with only an opaque ID cannot be
            # proven different from this candidate; stop for manual review.
            return str(other_id)
        other_identity = _job_identity(other_url)
        if other_identity == identity or (
            not other_identity and _url_host_key(other_url) == _url_host_key(job_url)
        ):
            return str(other_id)
    return None


def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resume_info(file_id: str) -> tuple[str, str]:
    # Resolve only registered, active files inside data/users. A path supplied by
    # the model must never be interpreted as a resume attachment.
    path = get_file_path(str(file_id).strip())
    return path.name, _file_sha256(path)


_RESUME_MODES = {"file_upload", "platform_profile"}


def _resume_mode(record: dict) -> str:
    # Existing saved applications and cards predate the delivery-mode field.
    return str(record.get("resume_delivery_mode") or "file_upload")


def _normalize_resume_binding(resume_file_id: str, resume_delivery_mode: str,
                              platform_resume_label: str) -> tuple[str, str, str, str]:
    mode = str(resume_delivery_mode or "file_upload").strip()
    file_id = str(resume_file_id or "").strip()
    label = " ".join(str(platform_resume_label or "").split())
    if mode not in _RESUME_MODES:
        raise ValueError("resume_delivery_mode 必须为 file_upload 或 platform_profile。")
    if mode == "file_upload":
        if not file_id or label:
            raise ValueError("file_upload 需要已登记的 resume_file_id，且不能指定站内简历标识。")
        name, digest = _resume_info(file_id)
        return file_id, digest, name, ""
    if file_id:
        raise ValueError("platform_profile 使用网站账户内的简历，不得绑定为本地文件上传。")
    if len(label) < 3 or len(label) > 120:
        raise ValueError("platform_profile 需要页面可见的准确简历标识（3 到 120 字符）。")
    return "", "", label, label


def _browser_observation(markers: dict[str, str], selector: str = "") -> dict:
    """Observe full visible DOM text without the 10k snapshot truncation."""
    selector = str(selector or "").strip()
    if selector.startswith("@"):
        raise ValueError("核验用 selector 必须是从页面观察到的 CSS 选择器，不能是快照 @ 引用。")
    normalized = {key: " ".join(str(value or "").split()) for key, value in markers.items()}
    code = Template("""(() => {
      const markers = $markers;
      const selector = $selector;
      const visible = el => Boolean(el && el.getClientRects().length);
      const nodes = selector
        ? Array.from(document.querySelectorAll(selector)).filter(visible)
        : (document.body ? [document.body] : []);
      const compact = value => String(value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
      const text = nodes.length === 1 ? compact(nodes[0].innerText) : '';
      const root = nodes.length === 1 ? nodes[0] : null;
      const selected = Boolean(root && selector && (
        root.checked === true || root.selected === true ||
        root.matches('[aria-selected="true"], [aria-checked="true"], [data-state="checked"], '
          + '[data-state="selected"], [data-state="active"], [data-selected="true"], [selected], :checked') ||
        root.classList.contains('selected') || root.classList.contains('checked')
      ));
      const hits = {};
      for (const [key, marker] of Object.entries(markers)) {
        hits[key] = Boolean(text && marker && text.includes(compact(marker)));
      }
      return JSON.stringify({url: String(location.href || ''),
        title: String(document.title || ''), scope_count: nodes.length,
        scope_text_length: text.length, selected, hits});
    })()""").substitute(
        markers=json.dumps(normalized, ensure_ascii=False),
        selector=json.dumps(selector, ensure_ascii=False),
    )
    observed = _evaluate_json(code)
    if not isinstance(observed, dict) or not isinstance(observed.get("hits"), dict):
        raise ValueError("无法读取当前页面的可见核验信息。")
    if observed.get("scope_count") != 1:
        raise ValueError("核验区域必须对应页面上唯一可见的元素。")
    return observed


def _observed_submit_target(selector: str, job_url: str, title: str, company: str) -> None:
    """Bind the one clickable submit control to the observed job context."""
    if not selector or selector.startswith("@") or selector.casefold() in {"body", "html", ":root", "main", "#app"}:
        raise ValueError("提交按钮须使用当前岗位页面观察到的具体 CSS 选择器。")
    code = Template("""(() => {
      const selector = $selector;
      const title = $title;
      const company = $company;
      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
      const visible = el => Boolean(el && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden' &&
        getComputedStyle(el).opacity !== '0' &&
        getComputedStyle(el).pointerEvents !== 'none' && !el.closest('[aria-hidden="true"]'));
      const matches = Array.from(document.querySelectorAll(selector)).filter(visible);
      const target = matches.length === 1 ? matches[0] : null;
      const actionable = Boolean(target && target.matches(
        'button, input[type="submit"], input[type="button"], [role="button"], a[href]'));
      const enabled = Boolean(actionable && !target.disabled && !target.matches(':disabled') &&
        target.getAttribute('aria-disabled') !== 'true' && !target.closest('[inert]'));
      const label = target ? clean(target.innerText || target.value ||
        target.getAttribute('aria-label') || target.getAttribute('title')) : '';
      const submitLabel = label.length <= 100 &&
        /投递|申请|应聘|立即沟通|发起沟通|apply|submit|send application|start chat|contact recruiter/i.test(label) &&
        !/退款|退费|取消|撤回|删除|withdraw|cancel|delete|remove|sign in|log in|登录|注册/i.test(label);
      const outsideJob = Boolean(target && target.closest(
        'nav, aside, [role="navigation"], [class*="sidebar"], [class*="side-bar"], [class*="recommend"]'));
      let context = false;
      for (let node = target && target.parentElement; node && node !== document.body; node = node.parentElement) {
        if (node === document.documentElement || /^(app|root|__next)$/i.test(node.id || '') ||
            node.getAttribute('role') === 'application') continue;
        const kind = String(node.id || '') + ' ' + String(node.className || '');
        const jobRegion = node.matches('form, article, section, main, [role="main"], [role="dialog"]') ||
          /job|position|application|apply/i.test(kind);
        if (!jobRegion) continue;
        const content = clean(node.innerText);
        if (content.length <= 12000 && content.includes(clean(title)) && content.includes(clean(company))) {
          context = true;
          break;
        }
      }
      return JSON.stringify({url: String(location.href || ''), count: matches.length,
        enabled, submitLabel, context, outsideJob});
    })()""").substitute(
        selector=_js_literal(selector), title=_js_literal(title), company=_js_literal(company),
    )
    observed = _evaluate_json(code)
    if not isinstance(observed, dict) or not _same_url(observed.get("url", ""), job_url):
        raise ValueError("核对提交按钮时，浏览器已离开已授权岗位页面。")
    if (observed.get("count") != 1 or observed.get("enabled") is not True
            or observed.get("submitLabel") is not True or observed.get("context") is not True
            or observed.get("outsideJob") is True):
        raise ValueError("提交按钮必须唯一、可用，且位于显示该岗位与公司的投递区域内。")


def _observed_platform_resume(label: str, selector: str, *, site_host: str = "",
                              require_selected: bool = False,
                              selected_text: str = "") -> str:
    if not str(selector or "").strip():
        raise ValueError("站内简历需要当前页面已观察到的唯一 CSS 选择器。")
    selected_text = " ".join(str(selected_text or "").split())
    if selected_text and (len(selected_text) < 2 or selected_text.casefold() in label.casefold()):
        raise ValueError("选中状态文字不能包含在简历名称中，且至少需要两个字符。")
    if selected_text and re.search(
        r"未选|未使用|未启用|未勾选|未激活|not\s+(?:selected|checked|in use)|unselected|unchecked",
        selected_text, re.I,
    ):
        raise ValueError("选中状态文字表示未选用，提交已停止。")
    if selected_text and not re.search(
        r"已选中|已选择|当前使用|正在使用|使用中|已启用|\bselected\b|\bchecked\b|\bin use\b",
        selected_text, re.I,
    ):
        raise ValueError("选中状态文字必须明确表示当前简历已被选用。")
    markers = {"resume": label}
    if selected_text:
        markers["selected"] = selected_text
    observed = _browser_observation(markers, selector)
    if site_host and not _host_matches(observed.get("url", ""), site_host):
        raise ValueError("站内简历标识不在已授权的招聘平台页面上。")
    if not observed["hits"].get("resume"):
        raise ValueError("当前页面选定的站内简历与授权标识不一致。")
    if not 0 < int(observed.get("scope_text_length") or 0) <= 500:
        raise ValueError("站内简历核验区域过宽；请选择只代表这一份简历的元素。")
    if require_selected and not (
        observed.get("selected") or (selected_text and observed["hits"].get("selected"))
    ):
        raise ValueError("当前页面未证明这份站内简历已选中，提交已停止。")
    return str(observed.get("url") or "")


def _application_key(job_id: str, job_url: str, resume_file_id: str, resume_sha256: str,
                     resume_delivery_mode: str = "file_upload", platform_resume_label: str = "",
                     retry_generation: int = 0) -> str:
    raw = "\0".join((job_id, job_url, resume_file_id, resume_sha256))
    if resume_delivery_mode == "platform_profile":
        raw += "\0platform_profile\0" + platform_resume_label
    if retry_generation:
        raw += "\0new-approval\0" + str(retry_generation)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _card_matches_application(card: dict, application: dict) -> bool:
    payload = card.get("payload") or {}
    return (
        card.get("action_type") == "JOB_SUBMISSION"
        and str(card.get("card_id") or "") == str(application.get("approval_card_id") or "")
        and _resume_mode(payload) == _resume_mode(application)
        and str(payload.get("platform_resume_label") or "") == str(application.get("platform_resume_label") or "")
        and all(str(payload.get(key) or "") == str(application.get(key) or "")
                for key in ("application_id", "job_id", "job_url", "resume_file_id", "resume_sha256"))
    )


def _matching_card(application: dict, cards: list[dict]) -> Optional[dict]:
    return next((card for card in cards if _card_matches_application(card, application)), None)


def _legacy_review_blocks(job_id: str, cards: list[dict]) -> bool:
    return any(
        card.get("action_type") == "JOB_APPROVE"
        and str((card.get("payload") or {}).get("job_id") or "") == job_id
        and card.get("status") in {"pending", "rejected"}
        for card in cards
    )


def _policy_allows(application: dict, cards: list[dict], task_context: dict,
                   all_jobs: dict) -> bool:
    if not task_context.get("schedule_id"):
        return False
    job_id = str(application.get("job_id") or "")
    if any(card.get("action_type") in {"JOB_APPROVE", "JOB_SUBMISSION"}
           and str((card.get("payload") or {}).get("job_id") or "") == job_id
           and card.get("status") in {"pending", "rejected"}
           for card in cards):
        # An explicit per-job rejection or still-pending question takes
        # precedence over a broader scheduled policy.
        return False
    return bool(schedule_policy_allows(application, cards, task_context, all_jobs))


def _is_submission_authorized(application: dict, cards: list[dict], task_context: dict,
                              all_jobs: Optional[dict] = None) -> bool:
    """One exact approved card, or a separately persisted scheduled policy."""
    if _legacy_review_blocks(str(application.get("job_id") or ""), cards):
        return False
    card = _matching_card(application, cards)
    if card is not None:
        if card.get("status") != "approved" or card.get("action_error"):
            return False
        workflow = card.get("workflow") or {}
        linked_task = str(workflow.get("task_id") or "")
        # A card without a saved task link cannot authorize a later browser
        # action: it could be replayed from an unrelated conversation.
        if not linked_task or linked_task != str(task_context.get("task_id") or ""):
            return False
        if str(task_context.get("source") or "") not in {"approval", "recovery"}:
            return False
        if str(card.get("card_id")) not in set(task_context.get("approval_card_ids") or []):
            return False
        return True
    if application.get("approval_card_id"):
        # A missing card is a broken approval record, not a reason to fall back
        # to a broad policy.
        return False
    return _policy_allows(application, cards, task_context, all_jobs or {})


def _application_report(job_id: str, job: Optional[dict]) -> str:
    if not job:
        return json.dumps({"job_id": job_id, "status": "NOT_FOUND"}, ensure_ascii=False)
    app = job.get("application") or {}
    report = {
        "job_id": job_id,
        "title": job.get("title"),
        "company": job.get("company"),
        "status": app.get("status") or "NOT_PREPARED",
        "application_id": app.get("application_id"),
        "job_url": app.get("job_url"),
        "resume_file_id": app.get("resume_file_id"),
        "resume_delivery_mode": _resume_mode(app),
        "platform_resume_label": app.get("platform_resume_label"),
        "approval_card_id": app.get("approval_card_id"),
        "prepared_at": app.get("prepared_at"),
        "attempted_at": app.get("attempted_at"),
        "verified_at": app.get("verified_at"),
        "schedule_id": app.get("schedule_id"),
        "evidence": app.get("evidence"),
        "error": app.get("error"),
    }
    return json.dumps(report, ensure_ascii=False, sort_keys=True)


def get_application_status(job_id: str) -> str:
    """Read durable outcome; APPROVED and ATTEMPTING are not submission proof."""
    return _application_report(str(job_id), JobRecordManager().get_job(str(job_id)))


def parse_and_process_jobs(raw_job_list: List[Dict[str, Any]], create_review_card: bool = True) -> str:
    """
    保存已经提取的岗位字段；可生成旧版岗位初审卡，不推断缺失字段。
    """
    if not isinstance(raw_job_list, list) or any(not isinstance(job, dict) for job in raw_job_list):
        raise ValueError("raw_job_list 必须是岗位字典的列表。")
    context = get_current_task()
    if create_review_card and context.get("source") == "schedule" and context.get("schedule_id"):
        current_policy = next(
            (card for card in read_cards()
             if card.get("action_type") == "SCHEDULE_AUTO_APPLY"
             and str((card.get("payload") or {}).get("schedule_id") or "") == str(context["schedule_id"])),
            None,
        )
        if (current_policy and current_policy.get("status") == "approved"
                and not current_policy.get("action_error")):
            create_review_card = False
    for job in raw_job_list:
        if not str(job.get("job_id") or job.get("url") or job.get("job_url") or "").strip():
            raise ValueError("每个岗位需要网页中实际的 job_id 或岗位 URL，不能凭岗位名称推断唯一标识。")
    manager = JobRecordManager()
    submitted = 0

    for job in raw_job_list:
        job_id = str(job.get("job_id") or job.get("url") or job.get("job_url")).strip()
        existing = manager.get_job(job_id)
        if existing and (existing.get("application") or existing.get("status") != "PREPARING_CARD"):
            manager.fill_missing_job_fields(job_id, job)
            continue

        # 解析完整元数据
        pub_date = job.get("publish_date", "未知")
        location = job.get("location", "未知")
        edu = job.get("education", "不限")
        exp = job.get("experience", "不限")

        if not create_review_card:
            manager.record_job(job_id, job, status="DISCOVERED")
            submitted += 1
            continue

        # Keep failed card creation retryable. A crash between these two stores
        # reuses the idempotent card rather than dropping or duplicating it.
        manager.record_job(job_id, job, status="PREPARING_CARD")

        # 推送至 Inbox 决策卡片
        content = (
            f"📍 地址：{location} | 📅 发布时间：{pub_date}\n"
            f"🎓 要求：{edu} / {exp} | 💰 薪资：{job.get('salary', '面议')}\n"
            f"🏢 公司：{job.get('company', '未知公司')} - {job.get('title', '未知岗位')}"
        )
        send_user_request(
            title=f"岗位投递确认：{job.get('company', '未知公司')}",
            content=content,
            action_type="JOB_APPROVE",
            payload={"job_id": job_id},
            idempotency_key="job-approval:" + job_id,
        )
        manager.update_job_status(job_id, "PENDING_APPROVAL", expected_status="PREPARING_CARD")
        submitted += 1

    if create_review_card:
        return f"已保存岗位数据，新增 {submitted} 张岗位初审卡；初审不授权具体简历投递。"
    return f"已保存 {submitted} 条新岗位记录，未生成岗位初审卡；投递前需选择已登记简历并取得具体授权。"


def prepare_job_application(job_id: str, resume_file_id: str = "", require_card: bool = True,
                            resume_delivery_mode: str = "file_upload",
                            platform_resume_label: str = "",
                            platform_resume_selector: str = "") -> str:
    """Bind one job to one registered resume and request exact submission approval.

    For a scheduled run, require_card=False is accepted only after a persisted,
    already-approved schedule policy authorizes this candidate. The user file is
    referenced by stable ID and never copied into recruitment storage.
    """
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("job_id 不能为空。")
    _require_local_resume_permission(resume_delivery_mode)
    resume_file_id, resume_sha256, resume_name, platform_resume_label = _normalize_resume_binding(
        resume_file_id, resume_delivery_mode, platform_resume_label
    )
    resume_delivery_mode = str(resume_delivery_mode or "file_upload").strip()
    observed_resume_url = ""
    if resume_delivery_mode == "platform_profile":
        observed_resume_url = _observed_platform_resume(
            platform_resume_label, platform_resume_selector
        )
    manager = JobRecordManager()
    context = get_current_task()
    with _JOBS_LOCK:
        all_jobs = manager.get_all_jobs()
        job = all_jobs.get(job_id)
        if not job:
            raise ValueError(f"岗位 {job_id} 尚未登记，请先保存真实岗位页面的信息。")
        if job.get("status") in {"SUBMITTED", "APPLIED"} and not job.get("application"):
            raise ValueError("此岗位已有历史已投递记录，请先核对平台记录，避免重复投递。")
        job_url = _job_url(job_id, job)
        if observed_resume_url and not _host_matches(
            observed_resume_url, _site_scope_host(job_url)
        ):
            raise ValueError("站内简历标识与目标岗位不在同一招聘平台。")
        if _legacy_review_blocks(job_id, read_cards()):
            raise PermissionError("此岗位的初审卡仍待处理或已被拒绝，不能准备具体投递。")
        conflict = _conflicting_job_with_attempt(job_id, job_url, all_jobs)
        if conflict:
            raise SubmissionNotConfirmedError(
                f"相同岗位网址已由记录 {conflict} 提交或处于未知状态；请先核对平台记录，不可重复投递。"
            )
        existing = job.get("application") or {}
        if existing.get("status") == "SUBMITTED":
            return _application_report(job_id, job)
        if existing.get("status") in {"ATTEMPTING", "UNKNOWN"}:
            raise SubmissionNotConfirmedError(
                f"岗位 {job_id} 投递状态为 {existing['status']}，须先核对招聘平台记录，不可自动重试。"
            )
        if existing.get("status") == "NOT_SUBMITTED" and (
            context.get("source") != "manual" or not require_card
        ):
            raise PermissionError("上次提交已核对为未投递；只有新的手动请求和新的逐岗位批准卡才能再次尝试。")
        generation = int(existing.get("retry_generation") or 0)
        if existing.get("status") == "NOT_SUBMITTED":
            generation += 1
        fingerprint = _application_key(job_id, job_url, resume_file_id, resume_sha256,
                                       resume_delivery_mode, platform_resume_label, generation)
        application_id = f"app_{fingerprint[:16]}"
        if (existing and existing.get("application_id") != application_id
                and existing.get("status") not in {"REJECTED", "PREPARED", "FAILED", "NOT_SUBMITTED"}):
            raise ValueError("此岗位已有另一份待处理的简历或投递记录；请先核对原审批与状态。")
        if existing.get("application_id") == application_id and existing.get("status") != "PREPARING_CARD":
            return _application_report(job_id, job)

        application = existing if existing.get("application_id") == application_id else {
            "application_id": application_id,
            "retry_generation": generation,
            "prior_attempts": list(existing.get("prior_attempts") or []) + ([{
                "application_id": existing.get("application_id"),
                "status": existing.get("status"),
                "attempted_at": existing.get("attempted_at"),
                "verified_at": existing.get("verified_at"),
                "evidence": existing.get("evidence"),
            }] if existing.get("status") == "NOT_SUBMITTED" else []),
            "job_id": job_id,
            "job_url": job_url,
            "title": str(job.get("title") or ""),
            "company": str(job.get("company") or ""),
            "resume_file_id": resume_file_id,
            "resume_sha256": resume_sha256,
            "resume_delivery_mode": resume_delivery_mode,
            "platform_resume_label": platform_resume_label,
            "platform_resume_selector": str(platform_resume_selector or "").strip(),
            "platform_resume_observed_url": observed_resume_url,
            "approval_card_id": "",
            "prepared_at": _now(),
            "attempted_at": None,
            "verified_at": None,
            "schedule_id": str(context.get("schedule_id") or ""),
            "run_job_id": str(context.get("job_id") or ""),
            "evidence": None,
            "error": None,
        }
        application = deepcopy(application)

        if (require_card and context.get("source") in {"schedule", "approval", "recovery"}
                and context.get("schedule_id")):
            candidate_jobs = deepcopy(all_jobs)
            candidate_jobs[job_id]["application"] = application
            if _policy_allows(application, read_cards(), context, candidate_jobs):
                require_card = False

        if not require_card:
            if not context.get("schedule_id"):
                raise PermissionError("无卡片投递准备仅允许已授权的日程任务。")
            candidate_jobs = deepcopy(all_jobs)
            candidate_jobs[job_id]["application"] = application
            if not _policy_allows(application, read_cards(), context, candidate_jobs):
                raise PermissionError("找不到覆盖此岗位和简历的有效日程投递授权。")
            application["status"] = "PREPARED"
            manager.set_application(job_id, application,
                                    expected_application_id=existing.get("application_id") if existing else None)
            return _application_report(job_id, manager.get_job(job_id))

        application["status"] = "PREPARING_CARD"
        manager.set_application(job_id, application,
                                expected_application_id=existing.get("application_id") if existing else None)
        card_key = "job-submission:" + fingerprint
        resume_description = (
            f"本地文件：{resume_name}（文件编号 {resume_file_id}），提交时上传这份文件。"
            if resume_delivery_mode == "file_upload" else
            f"招聘平台站内简历：{platform_resume_label}；已在 {observed_resume_url} 观察到标识，"
            "提交时必须再次核对所选简历，不上传本地文件。"
        )
        send_user_request(
            title=f"确认投递：{job.get('company') or '未知公司'} - {job.get('title') or '未知岗位'}",
            content=(f"岗位：{job.get('title') or '未知岗位'} / {job.get('company') or '未知公司'}\n"
                     f"岗位网址：{job_url}\n简历方式：{resume_description}\n"
                     "同意仅授权这一岗位使用上述简历方式；投递结果仍须由网站记录核对。"),
            action_type="JOB_SUBMISSION",
            payload={
                "application_id": application_id, "job_id": job_id, "job_url": job_url,
                "resume_file_id": resume_file_id, "resume_sha256": resume_sha256,
                "resume_delivery_mode": resume_delivery_mode,
                "platform_resume_label": platform_resume_label,
                "platform_resume_selector": str(platform_resume_selector or "").strip(),
                "platform_resume_observed_url": observed_resume_url,
            },
            idempotency_key=card_key,
        )
        card = next((item for item in read_cards() if item.get("idempotency_key") == card_key), None)
        if not card:
            raise RuntimeError("投递确认卡未能持久化；尚未获得投递授权。")
        application["approval_card_id"] = str(card["card_id"])
        application["status"] = "PENDING_APPROVAL"
        manager.set_application(job_id, application, expected_application_id=application_id,
                                expected_statuses={"PREPARING_CARD"})
        return _application_report(job_id, manager.get_job(job_id))


def set_application_approval_from_card(card: dict) -> bool:
    """Reflect one persisted decision in the application record, without submitting."""
    if not isinstance(card, dict) or card.get("action_type") != "JOB_SUBMISSION":
        return False
    job_id = str((card.get("payload") or {}).get("job_id") or "")
    if card.get("status") not in {"approved", "rejected"} or not job_id:
        return False
    actual = next((item for item in read_cards() if item.get("card_id") == card.get("card_id")), None)
    manager = JobRecordManager()
    job = manager.get_job(job_id)
    if not actual or not job:
        raise ValueError("投递审批卡或岗位记录不存在。")
    application = job.get("application") or {}
    if not _card_matches_application(actual, application) or actual.get("status") != card.get("status"):
        raise ValueError("投递审批卡与岗位、简历绑定信息不一致。")
    if application.get("status") in {"SUBMITTED", "ATTEMPTING", "UNKNOWN"}:
        return True
    updated = deepcopy(application)
    updated["status"] = "APPROVED" if actual["status"] == "approved" else "REJECTED"
    manager.set_application(job_id, updated, expected_application_id=application["application_id"],
                            expected_statuses={"PREPARING_CARD", "PENDING_APPROVAL", "APPROVED", "REJECTED", "FAILED"})
    return True


def submit_job_application(job_id: str, resume_file_id: str = "", submit_selector: str = "",
                           success_text: str = "", upload_selector: str = "", *,
                           resume_delivery_mode: str = "file_upload",
                           platform_resume_label: str = "",
                           platform_resume_selector: str = "",
                           platform_resume_selected_text: str = "",
                           attachment_ready_text: str = "",
                           receipt_selector: str = "",
                           receipt_wait_seconds: int = 12) -> str:
    """Click once for one authorized job; an ambiguous result remains UNKNOWN."""
    job_id = str(job_id or "").strip()
    resume_file_id = str(resume_file_id or "").strip()
    resume_delivery_mode = str(resume_delivery_mode or "file_upload").strip()
    platform_resume_label = " ".join(str(platform_resume_label or "").split())
    submit_selector = str(submit_selector or "").strip()
    upload_selector = str(upload_selector or "").strip()
    platform_resume_selector = str(platform_resume_selector or "").strip()
    platform_resume_selected_text = " ".join(str(platform_resume_selected_text or "").split())
    attachment_ready_text = " ".join(str(attachment_ready_text or "").split())
    receipt_selector = str(receipt_selector or "").strip()
    success_text = " ".join(str(success_text or "").split())
    if not job_id or not submit_selector:
        raise ValueError("job_id 和已观察到的 submit_selector 均不能为空。")
    if len(success_text) < 3:
        raise ValueError("success_text 必须是明确的投递成功提示，至少 3 个字符。")
    if (not receipt_selector or receipt_selector.startswith("@")
            or receipt_selector.casefold() in {"body", "html", ":root", "main", "#app"}):
        raise ValueError("需要从实际页面观察到的唯一回执 CSS 选择器，不能用整页文字确认投递。")
    try:
        receipt_wait_seconds = int(receipt_wait_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt_wait_seconds 必须是整数。") from exc
    if not 0 <= receipt_wait_seconds <= 30:
        raise ValueError("receipt_wait_seconds 必须在 0 到 30 秒之间。")
    if resume_delivery_mode not in _RESUME_MODES:
        raise ValueError("resume_delivery_mode 必须为 file_upload 或 platform_profile。")
    _require_local_resume_permission(resume_delivery_mode)
    if resume_delivery_mode == "file_upload":
        if not resume_file_id or not upload_selector or platform_resume_label or platform_resume_selector:
            raise ValueError("file_upload 需要已授权的 resume_file_id 和已观察到的 upload_selector。")
        if not attachment_ready_text:
            raise ValueError("file_upload 需要网站上传后可观察的附件就绪文字，防止处理未完成就提交。")
    elif resume_file_id or upload_selector or not platform_resume_label:
        raise ValueError("platform_profile 需要已授权的站内简历标识，不接受本地上传参数。")
    if attachment_ready_text and len(attachment_ready_text) < 3:
        raise ValueError("attachment_ready_text 至少需要 3 个字符。")

    manager = JobRecordManager()
    context = get_current_task()
    with _JOBS_LOCK, _SESSION_LOCK:
        job = manager.get_job(job_id)
        if not job:
            raise ValueError(f"岗位 {job_id} 尚未登记。")
        application = job.get("application") or {}
        if _resume_mode(application) != resume_delivery_mode:
            raise PermissionError("提交方式与审批卡或日程授权不一致。")
        if application.get("resume_file_id") != resume_file_id:
            raise PermissionError("提交简历与已授权的文件编号不一致。")
        if str(application.get("platform_resume_label") or "") != platform_resume_label:
            raise PermissionError("站内简历标识与已授权内容不一致。")
        if application.get("status") == "SUBMITTED":
            return _application_report(job_id, job)
        if application.get("status") in {"ATTEMPTING", "UNKNOWN"}:
            raise SubmissionNotConfirmedError(
                f"岗位 {job_id} 投递状态为 {application['status']}，须先核对平台记录；不可自动重试。"
            )
        if application.get("status") not in {"PENDING_APPROVAL", "APPROVED", "PREPARED", "FAILED"}:
            raise PermissionError("该岗位没有已准备且可核对的投递记录。")
        if resume_delivery_mode == "file_upload":
            _, current_sha256 = _resume_info(resume_file_id)
            if current_sha256 != application.get("resume_sha256"):
                raise PermissionError("简历内容在授权后发生变化，请重新准备并取得授权。")
        elif application.get("resume_sha256"):
            raise PermissionError("站内简历不能使用本地文件哈希作为授权证据。")
        job_url = _job_url(job_id, job)
        if job_url != application.get("job_url"):
            raise PermissionError("岗位网址在授权后发生变化，请重新核对。")
        all_jobs = manager.get_all_jobs()
        conflict = _conflicting_job_with_attempt(job_id, job_url, all_jobs)
        if conflict:
            raise SubmissionNotConfirmedError(
                f"相同岗位网址已由记录 {conflict} 提交或处于未知状态；请先核对平台记录，不可重复投递。"
            )
        if not _is_submission_authorized(application, read_cards(), context, all_jobs):
            raise PermissionError("没有与此岗位、简历及当前任务精确匹配的有效投递授权，或岗位初审被拒绝。")

        title = " ".join(str(job.get("title") or "").split())
        company = " ".join(str(job.get("company") or "").split())
        if len(title) < 2 or len(company) < 2:
            raise ValueError("岗位名称和公司名称必须可在实际岗位页核验。")
        before_markers = {"success": success_text, "title": title, "company": company}
        if not application.get("approval_card_id"):
            location = " ".join(str(job.get("location") or "").split())
            if len(location) < 2:
                raise ValueError("定时自动投递缺少可核验的工作地点。")
            before_markers["location"] = location
        if attachment_ready_text:
            before_markers["attachment_ready"] = attachment_ready_text
        before = _browser_observation(before_markers)
        if not _same_url(before.get("url", ""), job_url):
            raise ValueError(f"当前页面不是已授权岗位网址：{before.get('url') or '无页面'}。")
        for key in ("title", "company", "location"):
            if key in before_markers and not before["hits"].get(key):
                raise ValueError(f"当前岗位页没有显示已登记的{key}，提交已停止。")
        if before["hits"].get("success"):
            ambiguous = deepcopy(application)
            ambiguous.update(status="UNKNOWN", error="点击前页面已经显示成功标志，无法归因于本次投递。",
                             evidence={"page_url": before.get("url"), "success_text": success_text,
                                       "stage": "before_submit_click"})
            manager.set_application(job_id, ambiguous, expected_application_id=application["application_id"],
                                    expected_statuses={"PENDING_APPROVAL", "APPROVED", "PREPARED", "FAILED"})
            raise SubmissionNotConfirmedError(
                f"岗位 {job_id} 状态 UNKNOWN：点击前已有成功提示；请核对平台记录，不可自动重试。"
            )

        try:
            if resume_delivery_mode == "file_upload":
                if attachment_ready_text and before["hits"].get("attachment_ready"):
                    raise ValueError("上传前已存在附件就绪标志，无法证明它属于本次上传。")
                _require_local_resume_permission(resume_delivery_mode)
                _web_upload_unchecked(upload_selector, resume_file_id)
                _, uploaded_sha256 = _resume_info(resume_file_id)
                if uploaded_sha256 != application.get("resume_sha256"):
                    raise ValueError("简历在上传过程中发生变化；已停止提交。")
                after_upload = _browser_observation(
                    {"attachment_ready": attachment_ready_text} if attachment_ready_text else {}
                )
                if not _same_url(after_upload.get("url", ""), job_url):
                    raise ValueError("上传简历后离开了已授权的岗位页面；已停止提交。")
                if attachment_ready_text and not after_upload["hits"].get("attachment_ready"):
                    raise ValueError("页面没有出现已约定的附件就绪标志。")
            else:
                selected_selector = platform_resume_selector or str(application.get("platform_resume_selector") or "")
                _observed_platform_resume(
                    platform_resume_label, selected_selector,
                    site_host=_site_scope_host(job_url),
                    require_selected=True,
                    selected_text=platform_resume_selected_text,
                )
                selected_page = _browser_observation({"title": title, "company": company})
                if not _same_url(selected_page.get("url", ""), job_url):
                    raise ValueError("核对站内简历后离开了已授权的岗位页面。")
            _observed_submit_target(submit_selector, job_url, title, company)
            if not _is_submission_authorized(application, read_cards(), context, manager.get_all_jobs()):
                raise PermissionError("投递授权在点击前发生变化；已停止提交。")
        except Exception as exc:
            failed = deepcopy(application)
            failed.update(status="FAILED", error=f"提交按钮未点击：{exc}",
                          evidence={"page_url": before.get("url"), "stage": "resume_ready_check",
                                    "resume_delivery_mode": resume_delivery_mode})
            manager.set_application(job_id, failed, expected_application_id=application["application_id"],
                                    expected_statuses={"PENDING_APPROVAL", "APPROVED", "PREPARED", "FAILED"})
            raise SubmissionNotConfirmedError(
                f"岗位 {job_id} 状态 FAILED：提交按钮未点击；{exc}。请检查页面和简历后再处理。"
            ) from exc

        attempting = deepcopy(application)
        attempting.update(status="ATTEMPTING", attempted_at=_now(), error=None,
                          schedule_id=str(context.get("schedule_id") or application.get("schedule_id") or ""),
                          run_job_id=str(context.get("job_id") or ""),
                          evidence={"page_url": job_url, "stage": "before_submit_click",
                                    "resume_delivery_mode": resume_delivery_mode,
                                    "platform_resume_label": platform_resume_label,
                                    "platform_resume_selected_text": platform_resume_selected_text})
        manager.set_application(job_id, attempting, expected_application_id=application["application_id"],
                                expected_statuses={"PENDING_APPROVAL", "APPROVED", "PREPARED", "FAILED"})

        try:
            _web_click_unchecked(submit_selector)
            deadline = time.monotonic() + receipt_wait_seconds
            last_reason = "等待网站明确回执"
            while True:
                try:
                    after = _browser_observation(
                        {"success": success_text, "title": title, "company": company},
                        receipt_selector,
                    )
                    if not _host_matches(after.get("url", ""), _site_scope_host(job_url)):
                        raise ValueError("提交后离开了已授权的招聘平台。")
                    if after["hits"].get("success"):
                        associated = after["hits"].get("title") and after["hits"].get("company")
                        associated = associated and 0 < int(after.get("scope_text_length") or 0) <= 2000
                        if associated:
                            break
                        last_reason = "成功提示未与当前岗位名称、公司及岗位页面关联"
                    else:
                        last_reason = "尚未看到明确的投递成功提示"
                except Exception as exc:
                    last_reason = str(exc)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(last_reason)
                time.sleep(min(1.0, remaining))
            confirmed = deepcopy(attempting)
            confirmed.update(status="SUBMITTED", verified_at=_now(), error=None,
                             evidence={"success_text": success_text, "page_url": after.get("url"),
                                       "job_title": title, "company": company,
                                       "receipt_selector": receipt_selector,
                                       "resume_delivery_mode": resume_delivery_mode,
                                       "platform_resume_label": platform_resume_label,
                                       "platform_resume_selected_text": platform_resume_selected_text})
            manager.set_application(job_id, confirmed, expected_application_id=attempting["application_id"],
                                    expected_statuses={"ATTEMPTING"})
        except Exception as exc:
            uncertain = deepcopy(attempting)
            uncertain.update(status="UNKNOWN", error=f"提交已尝试，网站结果尚未确认：{exc}",
                             evidence={"page_url": job_url, "success_text": success_text,
                                       "stage": "after_submit_click",
                                       "resume_delivery_mode": resume_delivery_mode})
            manager.set_application(job_id, uncertain, expected_application_id=attempting["application_id"],
                                    expected_statuses={"ATTEMPTING"})
            raise SubmissionNotConfirmedError(
                f"岗位 {job_id} 状态 UNKNOWN：提交动作结果未确认；{exc}。请核对平台记录，不可自动重试。"
            ) from exc
        return _application_report(job_id, manager.get_job(job_id))


def reconcile_application_status(job_id: str, application_id: str, resolution: str,
                                 evidence_selector: str, evidence_text: str,
                                 expected_url_contains: str = "") -> str:
    """Resolve UNKNOWN from a unique, visible platform record for this job.

    An explicit not-submitted record becomes terminal NOT_SUBMITTED. It never
    reuses the old approval to retry a possibly completed browser action.
    """
    job_id = str(job_id or "").strip()
    application_id = str(application_id or "").strip()
    resolution = str(resolution or "").strip().lower()
    evidence_selector = str(evidence_selector or "").strip()
    evidence_text = " ".join(str(evidence_text or "").split())
    expected_url_contains = str(expected_url_contains or "").strip()
    if not job_id or not application_id or not evidence_selector or len(evidence_text) < 3:
        raise ValueError("需提供岗位、申请编号、唯一记录的 CSS 选择器和明确的状态文字。")
    if resolution not in {"submitted", "not_submitted"}:
        raise ValueError("resolution 必须为 submitted 或 not_submitted。")
    positive = bool(re.search(r"投递成功|已投递|申请成功|申请已提交|application submitted|(?<!not )\bapplied\b",
                              evidence_text, re.I))
    negative = bool(re.search(r"未投递|尚未投递|未提交|申请失败|投递失败|not submitted|not applied",
                              evidence_text, re.I))
    if (resolution == "submitted" and (not positive or negative)) or (
        resolution == "not_submitted" and (not negative or positive)
    ):
        raise ValueError("状态文字与所选核对结果不一致；请使用网站明确显示的投递或未投递记录。")
    task_context = get_current_task()
    if (task_context.get("schedule_id")
            and task_context.get("source") in {"schedule", "approval", "recovery"}):
        raise PermissionError("日程及其审批或恢复任务不能自行核销不确定投递结果；请单独核对平台记录。")
    manager = JobRecordManager()
    with _JOBS_LOCK:
        job = manager.get_job(job_id)
        if not job:
            raise ValueError(f"岗位 {job_id} 尚未登记。")
        application = job.get("application") or {}
        if application.get("application_id") != application_id:
            raise ValueError("申请编号与当前岗位记录不一致。")
        if application.get("status") not in {"ATTEMPTING", "UNKNOWN"}:
            raise ValueError("只有结果未明的申请允许通过平台记录核对。")
        title = " ".join(str(job.get("title") or "").split())
        company = " ".join(str(job.get("company") or "").split())
        if len(title) < 2 or len(company) < 2:
            raise ValueError("岗位记录缺少可用于核对的名称或公司。")
        observed = _browser_observation(
            {"evidence": evidence_text, "title": title, "company": company},
            evidence_selector,
        )
        if not _host_matches(observed.get("url", ""), _site_scope_host(_job_url(job_id, job))):
            raise ValueError("核对页面不在原招聘平台域名下。")
        if expected_url_contains and expected_url_contains.casefold() not in str(observed.get("url") or "").casefold():
            raise ValueError("核对页面网址与预期的平台记录页不一致。")
        if not all(observed["hits"].get(key) for key in ("evidence", "title", "company")):
            raise ValueError("同一条可见网站记录未同时显示此岗位、公司和明确结果。")
        if not 0 < int(observed.get("scope_text_length") or 0) <= 2000:
            raise ValueError("网站记录核验区域过宽，无法确认结果属于这个岗位。")
        updated = deepcopy(application)
        updated.update(
            status="SUBMITTED" if resolution == "submitted" else "NOT_SUBMITTED",
            verified_at=_now(), error=None,
            evidence={"type": "platform_record", "resolution": resolution,
                      "page_url": observed.get("url"), "record_selector": evidence_selector,
                      "record_text": evidence_text, "job_title": title, "company": company},
        )
        manager.set_application(job_id, updated, expected_application_id=application_id,
                                expected_statuses={"ATTEMPTING", "UNKNOWN"})
        return _application_report(job_id, manager.get_job(job_id))


def get_recruitment_summary() -> str:
    """
    [业务工具] 获取目前在招聘平台的巡航记录、待审批岗位与沟通状态汇总。
    """
    manager = JobRecordManager()
    jobs = manager.get_all_jobs()
    total = len(jobs)
    statuses = [(job.get("application") or {}).get("status") for job in jobs.values()]
    legacy_reviews = sum(1 for job in jobs.values() if not job.get("application")
                         and job.get("status") == "PENDING_APPROVAL")
    legacy_approved = sum(1 for job in jobs.values() if not job.get("application")
                          and job.get("status") == "APPROVED")
    waiting = statuses.count("PENDING_APPROVAL")
    approved = sum(
        1 for job in jobs.values()
        if (job.get("application") or {}).get("status") in {"APPROVED", "PREPARED"}
        and job.get("review_status") not in {"PENDING_APPROVAL", "REJECTED"}
    )
    blocked = sum(
        1 for job in jobs.values()
        if (job.get("application") or {}).get("status") in {"APPROVED", "PREPARED"}
        and job.get("review_status") in {"PENDING_APPROVAL", "REJECTED"}
    )
    submitted = statuses.count("SUBMITTED")
    not_submitted = statuses.count("NOT_SUBMITTED")
    unknown = statuses.count("UNKNOWN") + statuses.count("ATTEMPTING")
    failed = statuses.count("FAILED")
    jobs_summary = (f"【招聘数据统计】总记录岗位：{total} 个 | 旧版岗位初审待回复：{legacy_reviews} 个 | "
            f"旧版岗位初审通过：{legacy_approved} 个 | 具体投递待审批：{waiting} 个 | "
            f"已准备或批准但未提交：{approved} 个 | 初审阻止提交：{blocked} 个 | "
            f"网页确认已投递：{submitted} 个 | 核对未投递：{not_submitted} 个 | "
            f"结果待核对：{unknown} 个 | 提交前失败：{failed} 个。")
    return jobs_summary + "\n" + get_hr_auto_reply_summary()


def listen_hr_messages(raw_messages: List[Dict[str, Any]]) -> str:
    """
    [业务工具] 解析监听到的未读 HR 消息列表，并在消息决策中心生成推送提醒卡片。
    """
    new_count = 0
    reused_count = 0
    if not isinstance(raw_messages, list) or any(not isinstance(msg, dict) for msg in raw_messages):
        raise ValueError("raw_messages 必须是消息字典的列表。")
    for msg in raw_messages:
        hr_name = msg.get("hr_name", "未知 HR")
        company = msg.get("company", "未知公司")
        text = msg.get("text", "")

        content = f"🏢 公司：{company}\n👤 HR：{hr_name}\n💬 消息：{text}"
        identity = json.dumps(msg, ensure_ascii=False, sort_keys=True)
        result = send_user_request(
            title=f"收到 HR 新消息：{company}",
            content=content,
            options=["acknowledge"],
            action_type="HR_MESSAGE_REVIEW",
            payload=msg,
            resume_task_on_response=False,
            idempotency_key="hr-message:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        )
        if str(result).startswith("等价请求已存在"):
            reused_count += 1
        else:
            new_count += 1

    return f"HR 消息提醒：新建 {new_count} 张，复用已有 {reused_count} 张。"




# 定时自动投递授权：与逐岗位投递共用上方的岗位状态和审批卡片。
ACTION_TYPE = "SCHEDULE_AUTO_APPLY"
_ATTEMPT_STATUSES = {"ATTEMPTING", "UNKNOWN", "SUBMITTED", "NOT_SUBMITTED"}


def _file_digest(path: Path) -> str:
    return _file_sha256(path)


def _schedule_fingerprint(schedule: dict[str, Any]) -> str:
    meaningful = {
        key: schedule.get(key)
        for key in ("id", "owner", "title", "description", "time", "rule")
    }
    meaningful["file_ids"] = sorted(
        str(row.get("file_id")) for row in get_task_resources(str(schedule["id"]))
        if row.get("status") == "active" and row.get("file_id")
    )
    payload = json.dumps(meaningful, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_terms(values: list[str] | None, label: str, *, required: bool) -> list[str]:
    if values is None:
        values = []
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{label} 必须是文本列表。")
    terms = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if required and not terms:
        raise ValueError(f"请至少填写一个{label}，不能批准不限岗位的自动投递。")
    if len(terms) > 20 or any(len(term) > 80 for term in terms):
        raise ValueError(f"{label}数量或长度超出限制。")
    return terms


def _host_matches(url: str, approved_host: str) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    approved_host = str(approved_host or "").strip().lower().lstrip(".")
    return bool(host and approved_host and (host == approved_host or host.endswith("." + approved_host)))


def _term_matches(text: str, term: str) -> bool:
    term = str(term or "").strip()
    if not term:
        return False
    if term.isascii() and term.isalnum():
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text, re.I) is not None
    return term.casefold() in text.casefold()


def prepare_scheduled_auto_apply(
    schedule_id: str,
    resume_file_id: str = "",
    role_keywords: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    max_per_run: int = 3,
    site_host: str = "zhipin.com",
    resume_delivery_mode: str = "file_upload",
    platform_resume_label: str = "",
    platform_resume_selector: str = "",
) -> str:
    """Ask once before enabling a paused schedule to submit matching jobs.

    A broad or ambiguous request is rejected here rather than turning into an
    unlimited browser authorization. The approval card contains every boundary.
    """
    schedule_id = str(schedule_id or "").strip()
    _require_local_resume_permission(resume_delivery_mode)
    resume_file_id, resume_sha256, resume_name, platform_resume_label = _normalize_resume_binding(
        resume_file_id, resume_delivery_mode, platform_resume_label
    )
    resume_delivery_mode = str(resume_delivery_mode or "file_upload").strip()
    schedule = get_schedule_record(schedule_id)
    if not schedule or schedule.get("owner") != "agent":
        raise ValueError("只可为已存在的 Agent 日程设置自动投递授权。")
    if schedule.get("status") != "paused":
        raise ValueError("请先以 initial_status='paused' 创建日程，批准后才会启用。")
    role_keywords = _clean_terms(role_keywords, "岗位关键词", required=True)
    locations = _clean_terms(locations, "工作地点", required=False)
    try:
        max_per_run = int(max_per_run)
    except (TypeError, ValueError) as exc:
        raise ValueError("每次投递上限必须是整数。") from exc
    if not 1 <= max_per_run <= 10:
        raise ValueError("每次投递上限必须在 1 到 10 之间。")
    site_host = str(site_host or "").strip().lower().lstrip(".")
    if not site_host or urlsplit("https://" + site_host).hostname != site_host or "." not in site_host:
        raise ValueError("招聘平台域名无效。")

    observed_resume_url = ""
    if resume_delivery_mode == "file_upload":
        resources = get_task_resources(schedule_id)
        if resume_file_id not in {str(row.get("file_id")) for row in resources if row.get("status") == "active"}:
            raise ValueError("请先把选定简历的 file_id 关联到这个日程。")
    else:
        observed_resume_url = _observed_platform_resume(
            platform_resume_label, platform_resume_selector, site_host=site_host
        )
    payload = {
        "schedule_id": schedule_id,
        "schedule_fingerprint": _schedule_fingerprint(schedule),
        "resume_file_id": resume_file_id,
        "resume_sha256": resume_sha256,
        "resume_name": resume_name,
        "resume_delivery_mode": resume_delivery_mode,
        "platform_resume_label": platform_resume_label,
        "platform_resume_selector": str(platform_resume_selector or "").strip(),
        "platform_resume_observed_url": observed_resume_url,
        "role_keywords": role_keywords,
        "locations": locations,
        "max_per_run": max_per_run,
        "site_host": site_host,
    }
    policy_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    schedule_cards = [
        card for card in read_cards()
        if card.get("action_type") == ACTION_TYPE
        and str((card.get("payload") or {}).get("schedule_id") or "") == schedule_id
    ]
    latest = schedule_cards[0] if schedule_cards else None
    if latest and latest.get("payload") == payload and latest.get("status") == "pending":
        return f"同一范围的授权卡仍待处理，卡片 ID: {latest['card_id']}。"
    # A rejected vote or a manually paused schedule needs a fresh decision.
    # The inbox's idempotency key is permanent, so the generation is part of it.
    generation = sum(card.get("payload") == payload for card in schedule_cards) + 1
    resume_description = (
        f"本地简历：{resume_name}（{resume_file_id}）；到点时上传此文件。"
        if resume_delivery_mode == "file_upload" else
        f"平台站内简历：{platform_resume_label}；已在 {observed_resume_url} 观察到标识。"
        "到点时必须核对网站实际选中的简历，不上传本地文件。"
    )
    content = (
        f"日程：{schedule.get('title') or schedule_id}（{schedule_id}）\n"
        f"执行时间／规则：{schedule.get('time') or schedule.get('rule') or '未设置'}\n"
        f"任务内容：{str(schedule.get('description') or '').strip()[:300] or '按日程标题执行'}\n"
        f"简历方式：{resume_description}\n"
        f"招聘平台：{site_host}\n岗位关键词：{'、'.join(role_keywords)}\n"
        f"工作地点：{'、'.join(locations) if locations else '不限'}\n"
        f"每次运行最多投递：{max_per_run} 个\n"
        "同意后，符合以上条件的岗位可在该日程到点时自动提交；周期日程会按规则重复，直到暂停或取消。"
        "网页结果不明确时会暂停该岗位，不会盲目重试。"
    )
    return send_user_request(
        title="批准定时自动投递范围",
        content=content,
        options=["approve", "reject"],
        action_type=ACTION_TYPE,
        payload=payload,
        idempotency_key=f"schedule-auto-apply:{schedule_id}:{policy_hash}:v{generation}",
    )


def activate_scheduled_auto_apply(card: dict[str, Any]) -> str:
    """Called only after a recorded card decision; rejection leaves it paused."""
    if not isinstance(card, dict) or card.get("action_type") != ACTION_TYPE:
        raise ValueError("这不是定时自动投递授权卡。")
    payload = card.get("payload") or {}
    schedule_id = str(payload.get("schedule_id") or "")
    schedule = get_schedule_record(schedule_id)
    if not schedule:
        raise ValueError("关联日程不存在，授权未启用。")
    latest = next(
        (item for item in read_cards()
         if item.get("action_type") == ACTION_TYPE
         and str((item.get("payload") or {}).get("schedule_id") or "") == schedule_id),
        None,
    )
    if (not latest or latest.get("card_id") != card.get("card_id")
            or latest.get("status") != card.get("status") or latest.get("payload") != payload):
        raise ValueError("此日程已有更新的授权请求；请处理最新卡片。")
    if latest.get("status") != "approved":
        if schedule.get("status") == "active":
            update_schedule(schedule_id, new_status="paused")
        return "自动投递授权未批准；日程保持暂停。"
    if _schedule_fingerprint(schedule) != payload.get("schedule_fingerprint"):
        raise ValueError("日程内容或关联文件已变化；请重新提交授权卡。")
    if _resume_mode(payload) == "file_upload":
        if _file_digest(get_file_path(str(payload.get("resume_file_id") or ""))) != payload.get("resume_sha256"):
            raise ValueError("简历内容已变化；请重新提交授权卡。")
    elif not payload.get("platform_resume_label") or not payload.get("platform_resume_observed_url"):
        raise ValueError("站内简历授权缺少页面观察记录；请重新提交授权卡。")
    if schedule.get("status") == "paused":
        update_schedule(schedule_id, new_status="active")
    elif schedule.get("status") != "active":
        raise ValueError("关联日程当前无法启用，请检查日程状态。")
    return f"已批准并启用 Agent 日程 {schedule_id} 的限定范围自动投递。"


def schedule_policy_allows(
    application: dict[str, Any],
    cards: Optional[list[dict[str, Any]]],
    task_context: dict[str, Any],
    all_jobs: Optional[dict[str, Any]] = None,
) -> bool:
    """Check a previously approved policy against the actual scheduled run."""
    schedule_id = str(task_context.get("schedule_id") or "")
    run_job_id = str(task_context.get("job_id") or "")
    lineage = get_schedule_lineage(
        schedule_id, run_job_id, str(task_context.get("source") or ""),
        str(task_context.get("parent_job_id") or ""),
    )
    if not lineage:
        return False
    _, occurrence_job_ids = lineage
    cards = cards if cards is not None else read_cards()
    matching = [
        card for card in cards
        if card.get("action_type") == ACTION_TYPE
        and str((card.get("payload") or {}).get("schedule_id") or "") == schedule_id
    ]
    if (not matching or matching[0].get("status") != "approved"
            or matching[0].get("action_error")):
        return False  # A newer pending/rejected policy supersedes an old vote.
    payload = matching[0].get("payload") or {}
    schedule = get_schedule_record(schedule_id)
    if not schedule or schedule.get("owner") != "agent" or schedule.get("status") != "running":
        return False
    if str(schedule.get("job_id") or "") != run_job_id:
        return False
    if _schedule_fingerprint(schedule) != payload.get("schedule_fingerprint"):
        return False
    if _resume_mode(application) != _resume_mode(payload):
        return False
    if _resume_mode(payload) == "file_upload":
        resume_file_id = str(application.get("resume_file_id") or "")
        if resume_file_id != payload.get("resume_file_id"):
            return False
        if _file_digest(get_file_path(resume_file_id)) != payload.get("resume_sha256"):
            return False
        if application.get("resume_sha256") != payload.get("resume_sha256"):
            return False
    elif (not application.get("platform_resume_label")
          or application.get("platform_resume_label") != payload.get("platform_resume_label")
          or application.get("resume_file_id") or application.get("resume_sha256")):
        return False
    if not _host_matches(application.get("job_url"), str(payload.get("site_host") or "")):
        return False
    title = str(application.get("title") or "")
    if not title or not any(_term_matches(title, str(term)) for term in payload.get("role_keywords") or []):
        return False

    all_jobs = all_jobs if all_jobs is not None else {}
    job_id = str(application.get("job_id") or "")
    job = all_jobs.get(job_id) or {}
    places = payload.get("locations") or []
    if places and not any(str(place).casefold() in str(job.get("location") or "").casefold() for place in places):
        return False
    attempted = sum(
        1 for key, item in all_jobs.items()
        if str(key) != job_id
        and isinstance(item, dict)
        and isinstance(item.get("application"), dict)
        and item["application"].get("status") in _ATTEMPT_STATUSES
        and item["application"].get("schedule_id") == schedule_id
        and item["application"].get("run_job_id") in occurrence_job_ids
    )
    return attempted < int(payload.get("max_per_run") or 0)


def approved_schedule_policy_context(schedule_id: str) -> str:
    """Give a scheduled Agent the same bounded scope the user approved."""
    schedule_id = str(schedule_id or "").strip()
    if not schedule_id:
        return ""
    card = next(
        (item for item in read_cards()
         if item.get("action_type") == ACTION_TYPE
         and str((item.get("payload") or {}).get("schedule_id") or "") == schedule_id),
        None,
    )
    schedule = get_schedule_record(schedule_id)
    if (not card or card.get("status") != "approved"
            or card.get("action_error") or not schedule):
        return ""
    payload = card.get("payload") or {}
    try:
        if _schedule_fingerprint(schedule) != payload.get("schedule_fingerprint"):
            return "\n\n自动投递授权与当前日程内容不一致；不得提交，请重新取得授权。"
        resume_id = str(payload.get("resume_file_id") or "")
        if _resume_mode(payload) == "file_upload":
            if _file_digest(get_file_path(resume_id)) != payload.get("resume_sha256"):
                return "\n\n已授权简历的内容发生变化；不得提交，请重新取得授权。"
        elif not payload.get("platform_resume_label"):
            return "\n\n站内简历授权缺少准确标识；不得提交，请重新取得授权。"
    except (OSError, ValueError) as exc:
        return f"\n\n自动投递授权无法核验：{exc}。不得提交，请先检查用户文件。"
    resume_context = (
        f"本地简历 file_id={resume_id}，需要页面上传控件及上传后的附件就绪文字；"
        if _resume_mode(payload) == "file_upload" else
        f"站内简历标识={payload.get('platform_resume_label')}，先核对当前页面实际选中项，不上传本地文件；"
    )
    return (
        "\n\n此日程有已批准的自动投递范围："
        f"平台 {payload.get('site_host')}；岗位关键词 {'、'.join(payload.get('role_keywords') or [])}；"
        f"地点 {'、'.join(payload.get('locations') or []) or '不限'}；"
        f"每次最多 {payload.get('max_per_run')} 个；{resume_context}"
        "保存岗位时使用 parse_and_process_jobs(create_review_card=false)；"
        "只能对满足这些条件的岗位调用 prepare_job_application(require_card=false) 和 "
        "submit_job_application，最终回执要在同一唯一元素中包含成功文字、岗位名和公司名。"
        "结果为 UNKNOWN 或 FAILED 时停止本岗位，不要重复提交。"
    )


# HR reply policy, page observation, and durable send state.
HR_AUTO_REPLY_ACTION = "SCHEDULE_HR_AUTO_REPLY"
_HR_ATTEMPT_STATES = {"ATTEMPTING", "UNKNOWN", "OBSERVED_SENT"}
_HR_BLOCKED_STATES = _HR_ATTEMPT_STATES


class HRReplyNotConfirmedError(RuntimeError):
    """A click may have happened; the message must not be retried automatically."""


def _hr_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _hr_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _hr_host(value: str) -> str:
    host = str(value or "").strip().lower().lstrip(".")
    parsed = urlsplit("https://" + host)
    if not host or parsed.hostname != host or "." not in host:
        raise ValueError("招聘网站域名无效。")
    return host


def _hr_keywords(values: list[str]) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("消息关键词必须是文本列表。")
    terms = list(dict.fromkeys(_hr_text(value) for value in values if _hr_text(value)))
    if not terms or len(terms) > 20 or any(len(term) > 80 for term in terms):
        raise ValueError("请填写 1 到 20 个消息关键词，每个不超过 80 字符。")
    return terms


def _hr_card_for_schedule(schedule_id: str, cards: list[dict]) -> Optional[dict]:
    # send_user_request inserts new cards at the front. A newer pending or
    # rejected rule must revoke an older approved rule immediately.
    return next(
        (card for card in cards
         if card.get("action_type") == HR_AUTO_REPLY_ACTION
         and str((card.get("payload") or {}).get("schedule_id") or "") == schedule_id),
        None,
    )


def prepare_scheduled_hr_auto_reply(
    schedule_id: str,
    site_host: str,
    message_keywords: list[str],
    reply_text: str = "",
    max_per_run: int = 3,
    reply_mode: str = "exact",
    delivery_mode: str = "observe",
) -> str:
    """Create an independent decision card for one narrowly scoped HR reply rule.

    An Agent schedule may be paused or active when this card is requested. A
    paused schedule still needs a separate activation step after approval.
    """
    schedule_id = str(schedule_id or "").strip()
    schedule = get_schedule_record(schedule_id)
    if not schedule or schedule.get("owner") != "agent":
        raise ValueError("只能为已存在的 Agent 日程设置 HR 自动回复。")
    if schedule.get("status") not in {"paused", "active"}:
        raise ValueError("请在日程暂停或空闲时申请 HR 自动回复授权。")
    site_host = _hr_host(site_host)
    message_keywords = _hr_keywords(message_keywords)
    reply_mode = str(reply_mode or "exact").strip().lower()
    if reply_mode not in {"exact", "guided", "draft"}:
        raise ValueError("回复模式只能是 exact、guided 或 draft。")
    delivery_mode = str(delivery_mode or "observe").strip().lower()
    if reply_mode == "draft" and delivery_mode not in {"observe", "review", "auto"}:
        raise ValueError("自由拟稿的运行方式只能是 observe、review 或 auto。")
    if reply_mode != "draft" and delivery_mode != "observe":
        raise ValueError("delivery_mode 仅用于自由拟稿模式。")
    reply_text = str(reply_text or "")
    if reply_mode == "exact" and (not reply_text.strip() or len(reply_text) > 2000):
        raise ValueError("精确回复模式需要 1 到 2000 字符的文本。")
    if reply_mode in {"guided", "draft"} and reply_text.strip():
        raise ValueError("引导式或自由拟稿模式不能同时指定固定回复文本。")
    if reply_mode == "exact" and "*" in message_keywords:
        raise ValueError("精确文本模式不能对所有 HR 消息使用通配关键词。")
    try:
        max_per_run = int(max_per_run)
    except (TypeError, ValueError) as exc:
        raise ValueError("每次运行回复上限必须是整数。") from exc
    if not 1 <= max_per_run <= 10:
        raise ValueError("每次运行回复上限必须在 1 到 10 之间。")

    payload = {
        "schedule_id": schedule_id,
        "schedule_fingerprint": _schedule_fingerprint(schedule),
        "site_host": site_host,
        "message_keywords": message_keywords,
        "reply_text": reply_text,
        "reply_mode": reply_mode,
        "delivery_mode": delivery_mode if reply_mode == "draft" else "",
        "max_per_run": max_per_run,
    }
    cards = read_cards()
    latest = _hr_card_for_schedule(schedule_id, cards)
    if latest and latest.get("payload") == payload:
        if latest.get("status") == "pending":
            return f"同一范围的 HR 回复授权卡正在等待处理，卡片 ID: {latest['card_id']}。"
        if latest.get("status") == "approved" and not latest.get("action_error"):
            return f"同一范围的 HR 回复规则已经批准，卡片 ID: {latest['card_id']}。"
    generation = sum(
        1 for card in cards
        if card.get("action_type") == HR_AUTO_REPLY_ACTION
        and str((card.get("payload") or {}).get("schedule_id") or "") == schedule_id
    ) + 1
    policy_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    content = (
        f"日程：{schedule.get('title') or schedule_id}（{schedule_id}）\n"
        f"招聘网站：{site_host}\n"
        f"HR 消息关键词（命中任一个）：{'、'.join(message_keywords)}\n" +
        ("* 表示该网站当前会话的任意 HR 原文均可进入判断；无法核实的内容仍会发决策卡。\n"
         if "*" in message_keywords else "") +
        f"每次运行最多自动回复：{max_per_run} 条\n" +
        (f"批准的精确回复文本：\n{reply_text}\n" if reply_mode == "exact" else
         ("引导式回复：仅可依据已登记岗位 JD、明确保存的求职偏好和个人日程，"
          "先调用 prepare_hr_message_response 生成并锁定文本。\n" if reply_mode == "guided" else
          f"自由拟稿：模型根据观察与证据拟稿；运行方式 {delivery_mode}。"
          "observe 只留草稿，review 每条审批，auto 仅在证据和权限核验通过后发送；"
          "敏感或无法核实的内容仍需逐条审批。\n")) +
        "仅在该 Agent 日程运行、当前页面属于该网站、会话 URL 及 HR 原文和发件人"
        "均已核对时生效。仅含 HR 巡查的暂停日程在批准后自动启用；"
        "如同一日程还有待处理的自动投递授权，则暂时保持暂停。"
        "发送结果不明时停止该消息，不能自动重试。"
    )
    return send_user_request(
        title="批准定时 HR 自动回复范围",
        content=content,
        options=["approve", "reject"],
        action_type=HR_AUTO_REPLY_ACTION,
        payload=payload,
        resume_task_on_response=False,
        idempotency_key=f"schedule-hr-reply:{schedule_id}:{policy_hash}:v{generation}",
    )


def activate_scheduled_hr_auto_reply(card: dict[str, Any]) -> str:
    """Apply one persisted vote; activate a paused HR-only schedule after approval."""
    if not isinstance(card, dict) or card.get("action_type") != HR_AUTO_REPLY_ACTION:
        raise ValueError("这不是 HR 自动回复授权卡。")
    schedule_id = str((card.get("payload") or {}).get("schedule_id") or "")
    latest = _hr_card_for_schedule(schedule_id, read_cards())
    if not latest or latest.get("card_id") != card.get("card_id"):
        raise ValueError("此日程已有更新的 HR 自动回复授权卡；旧卡不可启用。")
    schedule = get_schedule_record(schedule_id)
    if not schedule or schedule.get("owner") != "agent":
        raise ValueError("关联的 Agent 日程不存在，HR 回复规则未启用。")
    if card.get("status") != "approved":
        return "HR 自动回复未批准；此规则不会发送消息。"
    if latest.get("status") != "approved":
        raise ValueError("HR 自动回复批准结果尚未持久化。")
    if _schedule_fingerprint(schedule) != (latest.get("payload") or {}).get("schedule_fingerprint"):
        raise ValueError("日程内容或关联资源已变化；请重新申请 HR 自动回复授权。")
    if schedule.get("status") not in {"paused", "active", "running", "awaiting_human"}:
        raise ValueError("关联日程当前无法执行，请检查日程状态。")
    if schedule.get("status") == "paused":
        auto_card = next((item for item in read_cards()
                          if item.get("action_type") == ACTION_TYPE
                          and str((item.get("payload") or {}).get("schedule_id") or "") == schedule_id), None)
        if auto_card and (auto_card.get("status") != "approved" or auto_card.get("action_error")):
            return (f"已批准 Agent 日程 {schedule_id} 的 HR 自动回复；"
                    "自动投递授权尚未批准，日程继续暂停。")
        update_schedule(schedule_id, new_status="active")
        return f"已批准 HR 自动回复并启用 Agent 日程 {schedule_id}。"
    return f"已批准 Agent 日程 {schedule_id} 的限定范围 HR 自动回复规则。"


def approved_hr_auto_reply_context(schedule_id: str) -> str:
    """Prompt fragment for ScheduleRunner; actual sending rechecks every limit."""
    schedule_id = str(schedule_id or "").strip()
    schedule = get_schedule_record(schedule_id)
    card = _hr_card_for_schedule(schedule_id, read_cards()) if schedule_id else None
    if (not schedule or not card or card.get("status") != "approved"
            or card.get("action_error")):
        return ""
    policy = card.get("payload") or {}
    if _schedule_fingerprint(schedule) != policy.get("schedule_fingerprint"):
        return "\n\nHR 自动回复授权与当前日程不一致；不得自动发送，请重新取得授权。"
    return (
        "\n\n此日程有独立批准的 HR 自动回复规则："
        f"网站 {policy.get('site_host')}；消息关键词 "
        f"{'、'.join(policy.get('message_keywords') or [])}；"
        f"每次最多 {policy.get('max_per_run')} 条。"
        "先按日程要求巡查该平台当前可见的未读会话；优先复用已打开的标签页。"
        "找不到真实消息时报告没有可处理的消息，不可臆造。"
        "从当前会话页面观察 URL、唯一会话主容器、发件人、收到消息容器和 HR 原文，"
        "使用页面中实际存在的 CSS 选择器。" +
        ("在 draft 模式下调用 prepare_hr_reply_draft，提供模型拟稿和每项事实的原文证据；"
         "返回 PREPARED 后可调用 auto_reply_hr_message；逐条审批的 REVIEW_PENDING 草稿获准后，"
         "续办任务可核对当前会话并直接发送已保存的原文。观察模式不得发送。" if
         str(policy.get("reply_mode") or "exact") == "draft" else
         "在 guided 模式下先调用 prepare_hr_message_response，取得 PREPARED 结果后才可调用 "
         "auto_reply_hr_message；exact 模式回复文本由批准卡提供，不能自行改写。") +
        "规则不匹配时只提醒；UNKNOWN 或 ATTEMPTING 的消息不能重试。"
    )


def _hr_conversation_identity(conversation_url: str) -> str:
    """Drop navigation tracking while retaining path and conversation query keys."""
    parts = urlsplit(str(conversation_url or ""))
    host = _url_host_key(conversation_url)
    if parts.scheme.lower() not in {"http", "https"} or not host or parts.username or parts.password:
        return ""
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/") or "/",
                       urlencode(_stable_query(parts.query)), ""))


def _same_hr_conversation_url(left: str, right: str) -> bool:
    identity = _hr_conversation_identity(left)
    return bool(identity and identity == _hr_conversation_identity(right))


def _hr_fingerprint(conversation_url: str, sender: str, message_text: str) -> str:
    # Equal text from the same sender in one conversation is deliberately
    # treated as the same message when the site exposes no stable message ID.
    raw = "\0".join((_hr_conversation_identity(conversation_url),
                     _hr_text(sender), _hr_text(message_text)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hr_rows(manager: JobRecordManager) -> dict[str, dict]:
    data = manager._read()
    rows = data.get("hr_auto_replies")
    if rows is None:
        return {}
    if not isinstance(rows, dict) or any(not isinstance(row, dict) for row in rows.values()):
        raise ValueError("招聘记录中的 HR 自动回复状态损坏；原文件已保留。")
    return rows


def _hr_store(manager: JobRecordManager, fingerprint: str, changes: dict[str, Any]) -> dict:
    # Caller holds _JOBS_LOCK. Re-read before every transition to retain jobs
    # and other reply records written by existing recruitment tools.
    data = manager._read()
    rows = data.setdefault("hr_auto_replies", {})
    if not isinstance(rows, dict):
        raise ValueError("招聘记录中的 HR 自动回复状态损坏；原文件已保留。")
    previous = rows.get(fingerprint) or {}
    if not isinstance(previous, dict):
        raise ValueError("该 HR 消息状态损坏；原文件已保留。")
    current = {**previous, **changes, "updated_at": _hr_now()}
    rows[fingerprint] = current
    data["updated_at"] = datetime.now().timestamp()
    manager._save(data)
    return deepcopy(current)


def get_hr_auto_reply_status(message_fingerprint: str) -> str:
    """Read the local state; OBSERVED_SENT is a page result, not a server receipt."""
    message_fingerprint = str(message_fingerprint or "").strip().lower()
    if len(message_fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in message_fingerprint):
        raise ValueError("请提供 64 位 HR 消息指纹。")
    with _JOBS_LOCK:
        row = _hr_rows(JobRecordManager()).get(message_fingerprint)
    return json.dumps(
        {"message_fingerprint": message_fingerprint, **(deepcopy(row) if row else {"status": "NOT_FOUND"})},
        ensure_ascii=False, sort_keys=True,
    )


def get_hr_auto_reply_summary(schedule_id: str = "") -> str:
    """Summarize locally observed replies and unresolved messages."""
    schedule_id = str(schedule_id or "").strip()
    with _JOBS_LOCK:
        rows = list(_hr_rows(JobRecordManager()).values())
    if schedule_id:
        rows = [row for row in rows if row.get("schedule_id") == schedule_id]
    statuses = [str(row.get("status") or "") for row in rows]
    return (
        f"【HR 自动回复】记录 {len(rows)} 条 | 页面观察到发送结果 "
        f"{statuses.count('OBSERVED_SENT')} 条 | 发送结果待核对 "
        f"{statuses.count('UNKNOWN') + statuses.count('ATTEMPTING')} 条 | "
        f"未自动发送 {statuses.count('NOT_SENT')} 条 | "
        f"已准备待发送 {statuses.count('PREPARED')} 条 | "
        f"仅观察草稿 {statuses.count('DRAFTED')} 条 | "
        f"草稿待审批 {statuses.count('REVIEW_PENDING')} 条 | "
        f"点击前失败 {statuses.count('PRE_CLICK_FAILED')} 条。"
    )


def hr_reply_run_outcomes(run_job_id: str) -> tuple[str, bool, bool]:
    """Return a brief per-run result for ChatManager, without message contents."""
    run_job_id = str(run_job_id or "").strip()
    if not run_job_id:
        return "", False, False
    with _JOBS_LOCK:
        rows = [
            row for row in _hr_rows(JobRecordManager()).values()
            if str(row.get("run_job_id") or "") == run_job_id
        ]
    if not rows:
        return "", False, False
    observed = sum(row.get("status") == "OBSERVED_SENT" for row in rows)
    uncertain = sum(row.get("status") in {"UNKNOWN", "ATTEMPTING"} for row in rows)
    pre_click = sum(row.get("status") == "PRE_CLICK_FAILED" for row in rows)
    prepared = sum(row.get("status") == "PREPARED" for row in rows)
    drafted = sum(row.get("status") == "DRAFTED" for row in rows)
    review_pending = sum(row.get("status") == "REVIEW_PENDING" for row in rows)
    not_sent = sum(row.get("status") == "NOT_SENT" for row in rows)
    unnotified = sum(row.get("status") == "NOT_SENT" and not row.get("reminder_sent") for row in rows)
    summary = (
        "HR 回复执行记录（依据本次运行保存的状态）："
        f"页面观察到发送结果 {observed} 条；结果待核对 {uncertain} 条；"
        f"点击前失败 {pre_click} 条；已准备但未发送 {prepared} 条；"
        f"仅观察草稿 {drafted} 条；待审批草稿 {review_pending} 条；"
        f"因规则不匹配未发送 {not_sent} 条。"
    )
    return summary, bool(uncertain or pre_click or prepared or review_pending or unnotified), bool(observed)


def _hr_observe_page(
    conversation_url: str, sender: str, message_text: str,
    sender_selector: str, message_selector: str, incoming_message_selector: str,
    reply_input_selector: str, send_selector: str, conversation_selector: str,
) -> None:
    """Require sender, inbound source and composer inside one observed conversation."""
    code = f"""(() => {{
      const conversationSelector = {_js_literal(conversation_selector)};
      const senderSelector = {_js_literal(sender_selector)};
      const messageSelector = {_js_literal(message_selector)};
      const incomingSelector = {_js_literal(incoming_message_selector)};
      const replyInputSelector = {_js_literal(reply_input_selector)};
      const sendSelector = {_js_literal(send_selector)};
      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const visible = element => Boolean(element && element.getClientRects().length &&
        getComputedStyle(element).visibility !== 'hidden');
      const roots = [...document.querySelectorAll(conversationSelector)].filter(visible)
        .filter(element => element !== document.body && element !== document.documentElement &&
          !/^(app|root|__next)$/i.test(element.id || '') &&
          element.getAttribute('role') !== 'application');
      const root = roots.length === 1 ? roots[0] : null;
      const sidebarInside = Boolean(root && [...root.querySelectorAll(
        'aside, [role="navigation"], [class*="sidebar"], [class*="side-bar"], '
        + '[class*="chat-list"], [class*="conversation-list"], [class*="contact-list"]')]
        .some(visible));
      const within = selector => root ? [...root.querySelectorAll(selector)].filter(visible) : [];
      const composer = within(replyInputSelector);
      const sendButton = within(sendSelector);
      const senders = within(senderSelector)
        .filter(element => clean(element.innerText || element.textContent) === clean({_js_literal(sender)}))
        .filter(element => !element.closest('nav, aside, [role="navigation"], '
          + '[class*="sidebar"], [class*="side-bar"], [class*="chat-list"], '
          + '[class*="conversation-list"], [class*="contact-list"]'));
      const incoming = within(incomingSelector)
        .filter(container => container !== root)
        .filter(container => !composer.some(element => container.contains(element)))
        .filter(container => !sendButton.some(element => container.contains(element)));
      const sourceMessages = within(messageSelector)
        .filter(element => clean(element.innerText || element.textContent) ===
          clean({_js_literal(message_text)}))
        .filter(element => incoming.some(container => container === element || container.contains(element)));
      return JSON.stringify({{
        url: String(location.href || ''),
        rootCount: roots.length, sidebarInside, senderCount: senders.length,
        incomingCount: incoming.length,
        messageCount: sourceMessages.length,
        composerCount: composer.length, sendCount: sendButton.length
      }});
    }})()"""
    result = _evaluate_json(code)
    if not isinstance(result, dict) or not _same_hr_conversation_url(result.get("url", ""), conversation_url):
        raise ValueError("当前浏览器会话 URL 已变化；停止 HR 自动回复。")
    if (result.get("rootCount") != 1 or result.get("sidebarInside") is True or
            result.get("senderCount") != 1 or
            result.get("messageCount") != 1 or result.get("incomingCount", 0) < 1 or
            result.get("composerCount") != 1 or result.get("sendCount") != 1):
        raise ValueError("未在同一唯一会话容器内核验 HR 发件人、入站原文与发送控件。")


def _hr_observe_input(selector: str) -> str:
    code = f"""(() => {{
      const elements = [...document.querySelectorAll({_js_literal(selector)})]
        .filter(element => element.getClientRects().length &&
          getComputedStyle(element).visibility !== 'hidden');
      const element = elements.length === 1 ? elements[0] : null;
      return JSON.stringify({{
        url: String(location.href || ''), count: elements.length,
        value: element ? String('value' in element ? element.value : element.innerText || '') : ''
      }});
    }})()"""
    result = _evaluate_json(code)
    if not isinstance(result, dict) or result.get("count") != 1:
        raise ValueError("回复输入框没有唯一匹配；停止发送。")
    return str(result.get("value") or "")


def _hr_observe_click_target(selector: str, conversation_url: str, conversation_selector: str) -> None:
    code = f"""(() => {{
      const roots = [...document.querySelectorAll({_js_literal(conversation_selector)})]
        .filter(element => element.getClientRects().length &&
          getComputedStyle(element).visibility !== 'hidden');
      const elements = [...document.querySelectorAll({_js_literal(selector)})]
        .filter(element => element.getClientRects().length &&
          getComputedStyle(element).visibility !== 'hidden' && !element.disabled &&
          !element.matches(':disabled') && element.getAttribute('aria-disabled') !== 'true');
      return JSON.stringify({{url: String(location.href || ''), count: elements.length,
        inConversation: roots.length === 1 && elements.length === 1 && roots[0].contains(elements[0])}});
    }})()"""
    result = _evaluate_json(code)
    if (not isinstance(result, dict) or not _same_hr_conversation_url(result.get("url", ""), conversation_url) or
            result.get("count") != 1 or result.get("inConversation") is not True):
        raise ValueError("发送按钮没有在当前会话唯一匹配；停止发送。")


def _hr_confirmation_present(selector: str, marker: str, conversation_url: str) -> bool:
    code = f"""(() => {{
      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const matched = [...document.querySelectorAll({_js_literal(selector)})]
        .filter(element => element.getClientRects().length &&
          getComputedStyle(element).visibility !== 'hidden')
        .some(element => clean(element.innerText || element.textContent).includes(clean({_js_literal(marker)})));
      return JSON.stringify({{url: String(location.href || ''), matched}});
    }})()"""
    result = _evaluate_json(code)
    if not isinstance(result, dict) or not _same_hr_conversation_url(result.get("url", ""), conversation_url):
        raise ValueError("当前浏览器会话 URL 已变化；发送结果无法归因。")
    return result.get("matched") is True


def _hr_rule_for_run(schedule_id: str, run_job_id: str, conversation_url: str,
                     message_text: str, rows: dict[str, dict]) -> tuple[Optional[dict], str]:
    context = get_current_task()
    if (str(context.get("schedule_id") or "") != schedule_id or
            str(context.get("job_id") or "") != run_job_id or not run_job_id):
        return None, "仅当前 Agent 日程的运行任务可以自动回复。"
    lineage = get_schedule_lineage(
        schedule_id, run_job_id, str(context.get("source") or ""),
        str(context.get("parent_job_id") or ""),
    )
    if not lineage:
        return None, "当前作业不属于已授权日程的同一次执行。"
    _, occurrence_job_ids = lineage
    schedule = get_schedule_record(schedule_id)
    if (not schedule or schedule.get("owner") != "agent" or
            schedule.get("status") != "running" or
            str(schedule.get("job_id") or "") != run_job_id):
        return None, "关联日程当前没有运行此任务。"
    card = _hr_card_for_schedule(schedule_id, read_cards())
    if not card or card.get("status") != "approved" or card.get("action_error"):
        return None, "此日程没有当前有效的 HR 自动回复批准卡。"
    policy = card.get("payload") or {}
    if _schedule_fingerprint(schedule) != policy.get("schedule_fingerprint"):
        return None, "日程或关联资源已改变，HR 自动回复需重新批准。"
    if not _host_matches(conversation_url, str(policy.get("site_host") or "")):
        return None, "当前会话不在批准的网站域名内。"
    keywords = policy.get("message_keywords") or []
    if not ((str(policy.get("reply_mode") or "exact") in {"guided", "draft"} and "*" in keywords)
            or any(_term_matches(message_text, term) for term in keywords)):
        return None, "HR 原文未命中批准的消息关键词。"
    attempted = sum(
        1 for row in rows.values()
        if row.get("schedule_id") == schedule_id and row.get("run_job_id") in occurrence_job_ids
        and row.get("status") in _HR_ATTEMPT_STATES
    )
    if attempted >= int(policy.get("max_per_run") or 0):
        return None, "本次日程已达到批准的 HR 自动回复数量上限。"
    return policy, ""


def _hr_reminder(fingerprint: str, conversation_url: str, sender: str,
                 message_text: str, reason: str) -> str:
    return send_user_request(
        title=f"HR 消息待查看：{_hr_text(sender)[:60]}",
        content=(f"会话：{conversation_url}\n发件人：{sender}\n"
                 f"HR 原文：{message_text}\n未自动回复：{reason}"),
        options=["acknowledge"],
        action_type="HR_MESSAGE_REVIEW",
        payload={"message_fingerprint": fingerprint, "conversation_url": conversation_url},
        resume_task_on_response=False,
        idempotency_key="hr-review:" + fingerprint,
    )


_HR_PERSONAL_TOPICS = {
    "travel": ("出差", "外派"),
    "six_day_week": ("做六休一", "单休", "六天", "6天", "周六上班"),
    "relocation": ("异地", "搬迁", "迁居", "换城市"),
}


def _hr_question_card(fingerprint: str, conversation_url: str, sender: str,
                      message_text: str, topic: str, reason: str) -> str:
    options = ["本次接受", "本次不接受", "暂不回答"] if topic in _HR_PERSONAL_TOPICS else ["我来处理", "暂不回复"]
    return send_user_request(
        title=f"请决定如何回复 HR：{sender[:40]}",
        content=f"会话：{conversation_url}\nHR 原文：{message_text}\n需要你判断：{reason}",
        options=options, action_type="HR_PREFERENCE_DECISION",
        payload={"message_fingerprint": fingerprint, "topic": topic,
                 "conversation_url": conversation_url},
        resume_task_on_response=topic in _HR_PERSONAL_TOPICS,
        idempotency_key="hr-decision:" + fingerprint,
    )


def _hr_choice(fingerprint: str) -> str:
    card = next((item for item in read_cards()
                 if item.get("action_type") == "HR_PREFERENCE_DECISION"
                 and (item.get("payload") or {}).get("message_fingerprint") == fingerprint), None)
    return str(card.get("user_choice") or "") if card and card.get("status") == "responded" else ""


def _time_matches_hr_date(message_text: str, start: datetime) -> bool:
    """Reject a model-proposed day that contradicts an explicit HR date."""
    text = _hr_text(message_text)
    today = datetime.now().astimezone().date()
    offsets = {"今天": 0, "明天": 1, "后天": 2}
    for label, offset in offsets.items():
        if label in text and start.date() != today + timedelta(days=offset):
            return False
    dated = re.search(r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?", text)
    if dated and ((int(dated.group(2)), int(dated.group(3))) != (start.month, start.day)
                  or (dated.group(1) and int(dated.group(1)) != start.year)):
        return False
    return True


def prepare_hr_message_response(
    conversation_url: str, sender: str, message_text: str, response_kind: str,
    job_id: str = "", jd_focus: str = "", proposed_start: str = "", proposed_end: str = "",
) -> str:
    """Plan one grounded reply; the model may classify, but cannot author sent text."""
    if not _hr_conversation_identity(conversation_url):
        raise ValueError("请提供当前 HR 会话的完整网址。")
    sender, message_text = str(sender or "").strip(), str(message_text or "").strip()
    if not sender or not message_text:
        raise ValueError("HR 发件人与原文不能为空。")
    kind = str(response_kind or "").strip().lower()
    fingerprint = _hr_fingerprint(conversation_url, sender, message_text)
    manager = JobRecordManager()
    with _JOBS_LOCK:
        prior = _hr_rows(manager).get(fingerprint) or {}
    if prior.get("status") in _HR_BLOCKED_STATES:
        return json.dumps({"message_fingerprint": fingerprint, "status": prior["status"],
                           "reason": "已有发送尝试；不能重新准备或自动重试。"}, ensure_ascii=False)
    if prior.get("status") == "PREPARED" and prior.get("response_kind") == kind:
        try:
            _guided_reply_still_valid(prior)
        except (ValueError, KeyError, TypeError):
            pass  # Changed preferences or calendar require a fresh proposal.
        else:
            return json.dumps({"message_fingerprint": fingerprint, "status": "PREPARED",
                               "reply_text": prior.get("reply_text"), "response_kind": kind},
                              ensure_ascii=False)

    reply = ""
    prepared = {"response_kind": kind, "job_id": str(job_id or "").strip()}
    reason = ""
    personal_topics = [topic for topic, terms in _HR_PERSONAL_TOPICS.items()
                       if any(term in message_text for term in terms)]
    if len(personal_topics) > 1:
        reason = "HR 同时询问多项个人取舍，不能只回答其中一项。"
    elif kind == "first_contact" and any(term in message_text for term in
                                          ("出差", "外派", "单休", "六休一", "异地", "搬迁",
                                           "薪资", "待遇", "期望", "面试", "有空", "接受", "能否", "是否")):
        reason = "HR 原文包含需要单独判断的问题，不能用首次沟通模板代替回答。"
    if reason:
        pass
    elif kind == "first_contact":
        job = manager.get_job(str(job_id or ""))
        if not job or not job.get("title") or not job.get("company"):
            reason = "缺少已核实的岗位名称或公司，无法生成基于 JD 的首次回复。"
        elif (job.get("application") or {}).get("status") != "SUBMITTED":
            reason = "尚无该岗位已投递的确切记录，不能假定已经向 HR 介绍过自己。"
        else:
            focus = str(jd_focus or "").strip()
            jd_text = str(job.get("jd_text") or "")
            if not jd_text:
                reason = "尚未登记岗位 JD 正文；请先从真实岗位页面获取描述。"
            elif not focus:
                reason = "请从已登记 JD 中选择一段真实的职责或要求作为首次沟通重点。"
            elif len(focus) > 80 or focus.casefold() not in jd_text.casefold():
                reason = "指定的 JD 重点未出现在已登记岗位描述中。"
            else:
                prepared["job_digest"] = hashlib.sha256(
                    json.dumps({key: job.get(key) for key in ("title", "company", "jd_text", "application")},
                               ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                reply = f"您好，感谢联系。我关注到贵司的{job['title']}岗位"
                if focus:
                    reply += f"，尤其对职位描述中提到的“{focus}”相关工作感兴趣"
                reply += "。我愿意进一步了解岗位职责和团队情况，期待交流。"
    elif kind in _HR_PERSONAL_TOPICS:
        if not any(term in message_text for term in _HR_PERSONAL_TOPICS[kind]):
            reason = "HR 原文没有明确涉及这个个人取舍话题。"
        elif any(term in message_text for term in ("长期", "频繁", "海外", "驻外", "每周", "全年")):
            reason = "HR 提及长期、频率或地域等额外条件，现有简单偏好不足以代答。"
        else:
            preference = load_runtime_settings()["recruitment_preferences"][kind]
            choice = _hr_choice(fingerprint)
            if preference == "ask" and choice in {"本次接受", "本次不接受"}:
                preference = "yes" if choice == "本次接受" else "no"
            if preference == "ask":
                reason = "尚未设置这一求职偏好；请在决策卡回答本次问题或到设置中保存长期偏好。"
            else:
                prepared["preference_value"] = preference
                prepared["preference_choice"] = choice
                topic = {"travel": "出差或外派", "six_day_week": "单休或做六休一", "relocation": "异地工作或搬迁"}[kind]
                reply = (f"关于{topic}，我可以接受，具体安排想再了解一下。" if preference == "yes"
                         else f"关于{topic}，我目前不考虑，感谢您提前说明。")
    elif kind == "courtesy_ack":
        if (any(term in message_text for term in ("吗", "?", "？", "是否", "能否", "请问", "薪资",
                                               "面试", "出差", "单休", "六休一", "异地", "搬迁")) or
                not any(term in message_text for term in ("收到", "感谢", "谢谢", "稍后联系", "保持联系")) or
                any(term in message_text for term in ("不合适", "未通过", "拒绝", "淘汰"))):
            reason = "原文不是单纯的礼貌确认，不能用客套话跳过实际问题。"
        else:
            reply = "收到，谢谢您的消息。期待后续交流。"
    elif kind == "interview_availability":
        if not any(term in message_text for term in ("面试", "时间", "有空", "方便", "约")):
            reason = "HR 原文没有提出面试时间问题。"
        else:
            try:
                start = datetime.fromisoformat(str(proposed_start).strip())
                end = datetime.fromisoformat(str(proposed_end).strip())
            except ValueError:
                reason = "请先从 HR 的日期要求与个人日历中确定准确的建议时段。"
            else:
                zone = datetime.now().astimezone().tzinfo
                start = start.replace(tzinfo=zone) if start.tzinfo is None else start.astimezone(zone)
                end = end.replace(tzinfo=zone) if end.tzinfo is None else end.astimezone(zone)
                window = load_runtime_settings()["interview_window"]
                if not window["start"]:
                    reason = "尚未设置允许自动提议的面试时段。"
                elif (start <= datetime.now().astimezone() or end <= start or
                      end - start > timedelta(hours=2) or start.date() != end.date()):
                    reason = "建议时段必须在未来、同一天且最长两小时。"
                elif not _time_matches_hr_date(message_text, start):
                    reason = "建议日期与 HR 原文中的日期不一致。"
                elif not (window["start"] <= start.strftime("%H:%M") and
                          end.strftime("%H:%M") <= window["end"]):
                    reason = "建议时段超出你在设置中明确允许的面试时间。"
                elif user_calendar_conflicts(start, end):
                    reason = "个人日历存在冲突或结束时间不明的安排，不能自动向 HR 承诺。"
                else:
                    prepared["proposed_start"] = start.isoformat(timespec="minutes")
                    prepared["proposed_end"] = end.isoformat(timespec="minutes")
                    reply = (f"您好，{start.month}月{start.day}日 {start:%H:%M}—{end:%H:%M} "
                             "我可以安排面试。请问这个时段方便吗？期待您的确认。")
    else:
        reason = "这类问题没有可核实的自动回复规则，需要你亲自决定。"

    if not reply:
        if kind == "first_contact" and reason in {
            "缺少已核实的岗位名称或公司，无法生成基于 JD 的首次回复。",
            "尚未登记岗位 JD 正文；请先从真实岗位页面获取描述。",
            "请从已登记 JD 中选择一段真实的职责或要求作为首次沟通重点。",
        }:
            return json.dumps({"message_fingerprint": fingerprint, "status": "NEEDS_DATA",
                               "reason": reason}, ensure_ascii=False)
        if kind == "interview_availability" and reason in {
            "请先从 HR 的日期要求与个人日历中确定准确的建议时段。",
            "建议时段必须在未来、同一天且最长两小时。",
            "建议日期与 HR 原文中的日期不一致。",
            "建议时段超出你在设置中明确允许的面试时间。",
            "个人日历存在冲突或结束时间不明的安排，不能自动向 HR 承诺。",
        }:
            return json.dumps({"message_fingerprint": fingerprint, "status": "TRY_ANOTHER_SLOT",
                               "reason": reason, "next_step": "先调用 find_interview_slots，选择符合 HR 日期要求的空档。"},
                              ensure_ascii=False)
        decision_topic = kind if kind in _HR_PERSONAL_TOPICS and reason.startswith("尚未设置") else "other"
        card = _hr_question_card(fingerprint, conversation_url, sender, message_text, decision_topic, reason)
        return json.dumps({"message_fingerprint": fingerprint, "status": "AWAITING_HUMAN",
                           "reason": reason, "card": card}, ensure_ascii=False)
    with _JOBS_LOCK:
        latest = _hr_rows(manager).get(fingerprint) or {}
        if latest.get("status") in _HR_BLOCKED_STATES:
            raise HRReplyNotConfirmedError("此 HR 消息已进入发送尝试状态，不能重新准备。")
        context = get_current_task()
        row = _hr_store(manager, fingerprint, {
            "status": "PREPARED", "message_fingerprint": fingerprint, "conversation_url": conversation_url,
            "sender": sender, "message_text": message_text, "reply_text": reply,
            "schedule_id": str(context.get("schedule_id") or ""),
            "run_job_id": str(context.get("job_id") or ""),
            **prepared,
        })
    return json.dumps({"message_fingerprint": fingerprint, "status": "PREPARED",
                       "reply_text": row["reply_text"], "response_kind": kind}, ensure_ascii=False)


def _guided_reply_still_valid(prepared: dict) -> None:
    kind = str(prepared.get("response_kind") or "")
    if kind in _HR_PERSONAL_TOPICS:
        current = load_runtime_settings()["recruitment_preferences"][kind]
        if current == "ask":
            choice = _hr_choice(str(prepared.get("message_fingerprint") or ""))
            current = "yes" if choice == "本次接受" else "no" if choice == "本次不接受" else "ask"
        if current != prepared.get("preference_value"):
            raise ValueError("求职偏好在准备回复后发生变化；需要重新审核。")
    if kind == "interview_availability":
        start = datetime.fromisoformat(str(prepared["proposed_start"]))
        end = datetime.fromisoformat(str(prepared["proposed_end"]))
        window = load_runtime_settings()["interview_window"]
        if (not window["start"] or start <= datetime.now().astimezone()
                or not window["start"] <= start.strftime("%H:%M")
                or not end.strftime("%H:%M") <= window["end"]
                or not _time_matches_hr_date(str(prepared.get("message_text") or ""), start)
                or user_calendar_conflicts(start, end)):
            raise ValueError("可约时段或个人日程在准备回复后发生变化；不能发送旧提议。")
    if kind == "first_contact":
        job = JobRecordManager().get_job(str(prepared.get("job_id") or ""))
        if not job or (job.get("application") or {}).get("status") != "SUBMITTED":
            raise ValueError("岗位投递状态发生变化；不能发送首次沟通模板。")
        digest = hashlib.sha256(
            json.dumps({key: job.get(key) for key in ("title", "company", "jd_text", "application")},
                       ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if digest != prepared.get("job_digest"):
            raise ValueError("岗位 JD 或投递记录在准备回复后发生变化；需要重新生成。")


_DRAFT_SENSITIVE_TERMS = (
    "薪资", "薪酬", "工资", "待遇", "奖金", "期望薪", "offer", "合同", "签约",
    "入职", "离职", "辞职", "加班", "单休", "六休一", "调休", "外派",
    "出差", "搬迁", "异地", "驻外", "婚育", "年龄", "身份证", "银行卡",
    "验证码", "密码", "转账", "付款", "保证", "一定能", "精通",
)


def _draft_evidence_snapshot(conversation_url: str, message_text: str,
                             evidence: list[dict], schedule_id: str) -> list[dict]:
    """Resolve every cited excerpt against canonical data; never accept model-provided source text alone."""
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8 or any(not isinstance(x, dict) for x in evidence):
        raise ValueError("请提供 1 到 8 项结构化证据，每项包含 source 和原文 quote。")
    linked = {str(row.get("file_id")) for row in get_task_resources(schedule_id)} if schedule_id else set()
    normalized = []
    has_message = False
    for item in evidence:
        source = str(item.get("source") or "").strip().lower()
        quote = _hr_text(item.get("quote"))
        if source != "preference" and (not 2 <= len(quote) <= 300):
            raise ValueError("每项文本证据的原文长度须为 2 到 300 字。")
        entry = {"source": source, "quote": quote}
        if source == "hr_message":
            if quote not in _hr_text(message_text):
                raise ValueError("HR 证据原文不在这条收到的消息中。")
            has_message = True
        elif source == "job":
            job_id = str(item.get("job_id") or "").strip()
            field = str(item.get("field") or "").strip()
            if field not in {"title", "company", "jd_text", "education", "experience", "location"}:
                raise ValueError("岗位证据必须指定可核实的 JD 或岗位字段。")
            job = JobRecordManager().get_job(job_id)
            if not job or not _host_matches(str(job.get("url") or job.get("job_url") or ""),
                                             urlsplit(conversation_url).hostname or ""):
                raise ValueError("岗位不存在或与当前 HR 会话网站不一致。")
            value = _hr_text(job.get(field))
            if quote not in value:
                raise ValueError("岗位证据原文未出现在已登记的字段中。")
            entry.update(job_id=job_id, field=field,
                         digest=hashlib.sha256(value.encode("utf-8")).hexdigest())
        elif source == "user_file":
            file_id = str(item.get("file_id") or "").strip()
            if not load_runtime_settings()["tool_permissions"].get("user_files", True) or file_id not in linked:
                raise PermissionError("用户文件须在当前日程中明确关联，并启用文件读取权限。")
            body = read_user_file(file_id).split("\n\n", 1)[-1]
            if quote not in _hr_text(body):
                raise ValueError("引用原文未出现在当前用户文件可读取的内容中。")
            entry.update(file_id=file_id, digest=hashlib.sha256(get_file_path(file_id).read_bytes()).hexdigest())
        elif source == "preference":
            key = str(item.get("key") or "").strip()
            value = load_runtime_settings()["recruitment_preferences"].get(key)
            if value not in {"yes", "no"} or quote != value:
                raise ValueError("求职偏好未明确保存为引用的接受或不接受。")
            entry.update(key=key, value=value)
        else:
            raise ValueError("证据来源只能是 hr_message、job、user_file 或 preference。")
        normalized.append(entry)
    if not has_message:
        raise ValueError("草稿至少需要一项当前 HR 消息的原文证据。")
    return normalized


def _draft_requires_review(message_text: str, draft_text: str,
                           evidence: list[dict], proposed_start: str) -> list[str]:
    """Conservative escalation. Citation existence does not prove a paraphrase is true."""
    reasons = []
    combined = f"{message_text}\n{draft_text}".casefold()
    found = [term for term in _DRAFT_SENSITIVE_TERMS if term.casefold() in combined]
    preference_keys = {item.get("key") for item in evidence if item.get("source") == "preference"}
    preference_terms = {"travel": {"出差", "外派"}, "six_day_week": {"单休", "六休一"},
                        "relocation": {"异地", "搬迁"}}
    uncovered = [term for term in found if not any(
        key in preference_keys and term in terms for key, terms in preference_terms.items()
    )]
    if uncovered:
        reasons.append("涉及需要你判断的条件：" + "、".join(uncovered[:5]))
    for item in evidence:
        if item["source"] != "preference":
            continue
        topic = {"travel": ("出差", "外派"), "six_day_week": ("单休", "六休一"),
                 "relocation": ("异地", "搬迁")}.get(item.get("key"), ())
        if not any(term in draft_text for term in topic):
            reasons.append("草稿没有清楚写明所引用的求职偏好话题。")
        if item.get("value") == "yes" and ("不接受" in draft_text or "不考虑" in draft_text or
                                            not any(term in draft_text for term in ("可以", "接受", "愿意"))):
            reasons.append("草稿与已保存的接受意向不一致。")
        if item.get("value") == "no" and not any(term in draft_text for term in ("不接受", "不考虑", "无法接受")):
            reasons.append("草稿没有清楚表达已保存的不接受意向。")
    for item in evidence:
        if item["source"] in {"job", "user_file"} and item["quote"] not in _hr_text(draft_text):
            reasons.append("草稿改写了文件或 JD 事实；仅原文引用可进入自动发送。")
            break
    factual_claim = re.compile(
        r"(?:我(?:曾|有|具有|具备|负责|参与|完成|熟悉|掌握|擅长|毕业|从事|开发|做过|使用过)"
        r"|(?:具备|熟悉|擅长|做过|参与过|负责过|毕业于|本科|硕士|博士|多年经验))"
    )
    cited_facts = [item["quote"] for item in evidence if item["source"] == "user_file"]
    for clause in re.split(r"[。！？；;\n]", draft_text):
        if factual_claim.search(clause) and not any(quote in _hr_text(clause) for quote in cited_facts):
            reasons.append("草稿含有未逐句核实的个人经历或能力表述。")
            break
    if any(item["source"] == "job" for item in evidence):
        reasons.append("岗位记录尚未与当前 HR 会话建立可靠绑定，涉及 JD 的回复需逐条审批。")
    if re.search(r"(?:1[3-9]\d{9})|(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", draft_text):
        reasons.append("草稿包含联系电话或邮箱地址。")
    source_text = " ".join(item["quote"] for item in evidence)
    if proposed_start:
        source_text += " " + proposed_start + " " + proposed_start.replace("T", " ")
    if any(token not in source_text for token in re.findall(r"\d+(?:\.\d+)?", draft_text)):
        reasons.append("草稿含有来源中未核实的数字。")
    if any(term in draft_text for term in ("主导", "独立完成", "多年经验", "丰富经验", "完全掌握")):
        reasons.append("草稿含有需要进一步核实的能力或经历表述。")
    if ("面试" in combined or "约时间" in combined) and not proposed_start:
        reasons.append("面试沟通没有经过日历时段核验。")
    if any(term in combined for term in ("长期", "频繁", "海外", "每周", "全年")):
        reasons.append("条件涉及持续时间、频率或地域细节。")
    if not proposed_start and any(term in draft_text for term in ("今天", "明天", "后天", "上午", "下午", "几点", "时段")):
        reasons.append("草稿提到具体时间或时段，但没有经过个人日历核验。")
    return list(dict.fromkeys(reasons))


def _draft_calendar_snapshot(message_text: str, draft_text: str,
                             proposed_start: str, proposed_end: str) -> dict:
    if not proposed_start and not proposed_end:
        return {}
    try:
        start = datetime.fromisoformat(str(proposed_start).strip())
        end = datetime.fromisoformat(str(proposed_end).strip())
    except ValueError as exc:
        raise ValueError("面试建议必须提供有效的开始和结束时间。") from exc
    zone = datetime.now().astimezone().tzinfo
    start = start.replace(tzinfo=zone) if start.tzinfo is None else start.astimezone(zone)
    end = end.replace(tzinfo=zone) if end.tzinfo is None else end.astimezone(zone)
    window = load_runtime_settings()["interview_window"]
    if (not window["start"] or start <= datetime.now().astimezone() or
            end <= start or end - start > timedelta(hours=2) or start.date() != end.date() or
            not window["start"] <= start.strftime("%H:%M") or
            not end.strftime("%H:%M") <= window["end"] or
            not _time_matches_hr_date(message_text, start) or user_calendar_conflicts(start, end)):
        raise ValueError("建议的面试时段与 HR 日期、个人日程或已保存的可约范围不一致。")
    if (f"{start.month}月{start.day}日" not in draft_text and
            start.strftime("%Y-%m-%d") not in draft_text):
        raise ValueError("草稿须明确写出建议的面试日期。")
    if start.strftime("%H:%M") not in draft_text:
        raise ValueError("草稿须明确写出建议的开始时间。")
    return {"proposed_start": start.isoformat(timespec="minutes"),
            "proposed_end": end.isoformat(timespec="minutes")}


def _draft_hash(draft_text: str, evidence: list[dict], calendar: dict) -> str:
    data = {"draft_text": draft_text, "evidence": evidence, "calendar": calendar}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _hr_draft_card(fingerprint: str, conversation_url: str, sender: str,
                   message_text: str, draft_text: str, evidence: list[dict],
                   draft_hash: str, reasons: list[str], *, observe: bool) -> str:
    sources = "\n".join(f"- {item['source']}：{item['quote'][:120]}" for item in evidence)
    return send_user_request(
        title=("HR 草稿观察记录" if observe else f"审批 HR 回复草稿：{sender[:35]}"),
        content=(f"会话：{conversation_url}\nHR 原文：{message_text}\n\n拟回复：\n{draft_text}\n\n"
                 f"引用证据：\n{sources}\n\n" +
                 (("观察时发现：" if observe else "需要审批：") + "；".join(reasons)
                  if reasons else "仅观察，不发送。" if observe else "请确认是否发送以上原文。")),
        options=["acknowledge"] if observe else ["approve", "reject"],
        action_type="HR_DRAFT_OBSERVE" if observe else "HR_DRAFT_APPROVE",
        payload={"message_fingerprint": fingerprint, "draft_hash": draft_hash,
                 "conversation_url": conversation_url},
        resume_task_on_response=not observe,
        idempotency_key=("hr-draft-observe:" if observe else "hr-draft-review:") + fingerprint,
    )


def prepare_hr_reply_draft(
    conversation_url: str, sender: str, message_text: str, draft_text: str,
    evidence: list[dict], proposed_start: str = "", proposed_end: str = "",
) -> str:
    """Save a model-authored reply with verified sources; policy decides observe/review/auto."""
    conversation_url, sender = str(conversation_url or "").strip(), str(sender or "").strip()
    message_text, draft_text = str(message_text or "").strip(), str(draft_text or "").strip()
    if not _hr_conversation_identity(conversation_url) or not sender or not message_text:
        raise ValueError("需要当前 HR 会话网址、发件人与收到的原文。")
    if not 2 <= len(draft_text) <= 600 or any(ord(ch) < 32 and ch not in "\n\t" for ch in draft_text):
        raise ValueError("草稿必须为 2 到 600 字的正常文本。")
    fingerprint = _hr_fingerprint(conversation_url, sender, message_text)
    context = get_current_task()
    schedule_id, run_job_id = str(context.get("schedule_id") or ""), str(context.get("job_id") or "")
    checked_evidence = _draft_evidence_snapshot(conversation_url, message_text, evidence, schedule_id)
    calendar = _draft_calendar_snapshot(message_text, draft_text, proposed_start, proposed_end)
    digest = _draft_hash(draft_text, checked_evidence, calendar)
    reasons = _draft_requires_review(message_text, draft_text, checked_evidence, proposed_start)
    with _JOBS_LOCK:
        manager = JobRecordManager()
        rows = _hr_rows(manager)
        prior = rows.get(fingerprint) or {}
        if prior.get("status") in _HR_BLOCKED_STATES:
            return json.dumps({"status": prior["status"], "message_fingerprint": fingerprint,
                               "reason": "已有发送尝试，不能自动重试。"}, ensure_ascii=False)
        if prior.get("status") in {"DRAFTED", "REVIEW_PENDING", "PREPARED"} and prior.get("draft_hash") != digest:
            raise ValueError("这条 HR 消息已有待处理的不同草稿；先处理原草稿。")
        policy, gate_reason = _hr_rule_for_run(schedule_id, run_job_id, conversation_url, message_text, rows)
        if policy and str(policy.get("reply_mode") or "exact") != "draft":
            policy, gate_reason = None, "此日程未批准自由拟稿模式。"
        delivery = str((policy or {}).get("delivery_mode") or "observe")
        review_card = next((card for card in read_cards()
                            if card.get("action_type") == "HR_DRAFT_APPROVE"
                            and (card.get("payload") or {}).get("message_fingerprint") == fingerprint), None)
        approved = bool(review_card and review_card.get("status") == "approved"
                        and (review_card.get("payload") or {}).get("draft_hash") == digest
                        and not review_card.get("action_error"))
        rejected = bool(review_card and review_card.get("status") == "rejected")
        if rejected:
            return json.dumps({"status": "NOT_SENT", "message_fingerprint": fingerprint,
                               "reason": "你已拒绝这条草稿。"}, ensure_ascii=False)
        status = ("DRAFTED" if delivery == "observe" or not policy else
                  "PREPARED" if approved or (delivery == "auto" and not reasons) else "REVIEW_PENDING")
        row = _hr_store(manager, fingerprint, {
            "status": status, "draft_version": 1, "message_fingerprint": fingerprint,
            "conversation_url": conversation_url, "sender": sender, "message_text": message_text,
            "reply_text": draft_text, "draft_sources": checked_evidence, "draft_hash": digest,
            "draft_policy_hash": (hashlib.sha256(json.dumps(policy, ensure_ascii=False, sort_keys=True)
                                                  .encode("utf-8")).hexdigest() if policy else ""),
            "review_reasons": reasons, "schedule_id": schedule_id, "run_job_id": run_job_id,
            "response_kind": "interview_availability" if calendar else "model_draft", **calendar,
        })
    if status == "PREPARED":
        return json.dumps({"status": status, "message_fingerprint": fingerprint,
                           "draft_text": draft_text, "evidence_count": len(checked_evidence)}, ensure_ascii=False)
    if status == "DRAFTED":
        notice = _hr_draft_card(fingerprint, conversation_url, sender, message_text,
                                draft_text, checked_evidence, digest, reasons, observe=True)
        return json.dumps({"status": "DRAFTED", "message_fingerprint": fingerprint,
                           "draft_text": draft_text, "notice": notice,
                           "reason": gate_reason or "仅观察，不发送。"}, ensure_ascii=False)
    card = _hr_draft_card(fingerprint, conversation_url, sender, message_text,
                          draft_text, checked_evidence, digest, reasons, observe=False)
    return json.dumps({"status": "AWAITING_HUMAN", "message_fingerprint": fingerprint,
                       "draft_text": draft_text, "card": card}, ensure_ascii=False)


def _draft_ready_for_send(row: dict, policy: dict, fingerprint: str,
                          schedule_id: str) -> None:
    if row.get("status") not in {"PREPARED", "REVIEW_PENDING"} or row.get("draft_version") != 1:
        raise ValueError("这条 HR 消息没有经过新版自由拟稿证据核验。")
    policy_hash = hashlib.sha256(json.dumps(policy, ensure_ascii=False, sort_keys=True)
                                 .encode("utf-8")).hexdigest()
    if row.get("schedule_id") != schedule_id or row.get("draft_policy_hash") != policy_hash:
        raise ValueError("草稿对应的日程或批准范围已经变化；必须重新拟稿。")
    delivery = str(policy.get("delivery_mode") or "observe")
    if delivery == "observe":
        raise ValueError("观察模式只记录草稿，不允许发送。")
    sources = _draft_evidence_snapshot(str(row.get("conversation_url") or ""),
                                       str(row.get("message_text") or ""),
                                       row.get("draft_sources"), str(row.get("schedule_id") or ""))
    calendar = _draft_calendar_snapshot(str(row.get("message_text") or ""),
                                        str(row.get("reply_text") or ""),
                                        str(row.get("proposed_start") or ""),
                                        str(row.get("proposed_end") or ""))
    digest = _draft_hash(str(row.get("reply_text") or ""), sources, calendar)
    if digest != row.get("draft_hash"):
        raise ValueError("引用文件、岗位信息、偏好或日程已改变；旧草稿不能发送。")
    reasons = _draft_requires_review(str(row.get("message_text") or ""),
                                     str(row.get("reply_text") or ""), sources,
                                     str(row.get("proposed_start") or ""))
    card = next((item for item in read_cards()
                 if item.get("action_type") == "HR_DRAFT_APPROVE"
                 and (item.get("payload") or {}).get("message_fingerprint") == fingerprint), None)
    approved = bool(card and card.get("status") == "approved"
                    and (card.get("payload") or {}).get("draft_hash") == digest
                    and not card.get("action_error"))
    if not approved and (delivery != "auto" or reasons):
        raise ValueError("草稿需要逐条审批；当前没有匹配的已批准决策卡。")


def record_hr_draft_decision_from_card(card: dict) -> None:
    """Apply a saved per-message vote without sending anything to the website."""
    if card.get("action_type") != "HR_DRAFT_APPROVE" or card.get("status") not in {"approved", "rejected"}:
        raise ValueError("这不是已处理的 HR 草稿审批卡。")
    payload = card.get("payload") or {}
    fingerprint = str(payload.get("message_fingerprint") or "")
    digest = str(payload.get("draft_hash") or "")
    with _JOBS_LOCK:
        manager = JobRecordManager()
        row = _hr_rows(manager).get(fingerprint)
        if not row or row.get("draft_hash") != digest or row.get("draft_version") != 1:
            raise ValueError("审批卡与已保存的 HR 草稿不一致，未更改回复状态。")
        if card["status"] == "approved" and row.get("status") not in {
            "REVIEW_PENDING", "PREPARED", "OBSERVED_SENT"
        }:
            raise ValueError("这条草稿已不处于可批准状态。")
        if card["status"] == "rejected" and row.get("status") not in {"REVIEW_PENDING", "NOT_SENT"}:
            raise ValueError("这条草稿已不处于可拒绝状态。")
        if card["status"] == "rejected" and row.get("status") == "REVIEW_PENDING":
            _hr_store(manager, fingerprint, {
                "status": "NOT_SENT", "error": "用户拒绝了这条 HR 回复草稿。",
                "reminder_sent": True,
            })


def record_confirmed_interview(
    conversation_url: str, sender: str, confirmation_message: str,
    proposal_message_fingerprint: str,
    sender_selector: str, message_selector: str, incoming_message_selector: str,
    reply_input_selector: str, send_selector: str, conversation_selector: str,
) -> str:
    """Book only after a previously sent proposal receives explicit same-time HR confirmation."""
    sender = str(sender or "").strip()
    confirmation_message = str(confirmation_message or "").strip()
    if not sender or not confirmation_message or not _hr_conversation_identity(conversation_url):
        raise ValueError("需要真实的 HR 发件人、确认原文和会话网址。")
    selectors = (sender_selector, message_selector, incoming_message_selector,
                 reply_input_selector, send_selector, conversation_selector)
    if any(not isinstance(value, str) or not value.strip() or value.startswith("@") for value in selectors):
        raise ValueError("请提供当前会话中已观察到的真实 CSS 选择器。")
    with _JOBS_LOCK, _SESSION_LOCK:
        page_url = str(_page_info().get("url") or "")
        if not _same_hr_conversation_url(page_url, conversation_url):
            raise ValueError("当前浏览器页面不是指定 HR 会话。")
        _hr_observe_page(conversation_url, sender, confirmation_message,
                         sender_selector, message_selector, incoming_message_selector,
                         reply_input_selector, send_selector, conversation_selector)
        manager = JobRecordManager()
        proposal = _hr_rows(manager).get(str(proposal_message_fingerprint or "")) or {}
        if (proposal.get("status") != "OBSERVED_SENT" or
                proposal.get("response_kind") != "interview_availability" or
                not _same_hr_conversation_url(str(proposal.get("conversation_url") or ""), conversation_url) or
                _hr_text(proposal.get("sender")) != _hr_text(sender) or
                _hr_text(proposal.get("message_text")) == _hr_text(confirmation_message)):
            raise ValueError("找不到同一会话中已发送且页面确认的面试时间提议。")
        start = datetime.fromisoformat(str(proposal["proposed_start"]))
        end = datetime.fromisoformat(str(proposal["proposed_end"]))
        text = _hr_text(confirmation_message)
        accepted = any(word in text for word in ("可以", "确认", "确定", "安排", "没问题", "好的"))
        rejected = any(word in text for word in ("不可以", "不行", "不太", "可能不", "暂时",
                                               "取消", "改期", "没空", "不方便", "不合适", "另约",
                                               "吗", "？", "?", "能否", "是否", "请问"))
        today = datetime.now().astimezone().date()
        relative = (("今天" in text and start.date() == today) or
                    ("明天" in text and start.date() == today + timedelta(days=1)) or
                    ("后天" in text and start.date() == today + timedelta(days=2)))
        date_confirmed = (f"{start.month}月{start.day}日" in text or
                          start.strftime("%Y-%m-%d") in text or relative)
        hour = start.hour
        chinese_hours = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六",
                         7: "七", 8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二"}
        spoken = hour - 12 if hour > 12 else hour
        hour_confirmed = (start.strftime("%H:%M") in text or
                          (start.minute == 0 and f"{hour}点" in text) or
                          (start.minute == 0 and hour > 12 and f"下午{hour - 12}点" in text) or
                          (start.minute == 0 and hour < 12 and f"上午{hour}点" in text) or
                          (start.minute == 0 and spoken in chinese_hours and
                           (f"{'下午' if hour >= 12 else '上午'}{chinese_hours[spoken]}点" in text)))
        if (not accepted or rejected or not date_confirmed or not hour_confirmed or
                not _time_matches_hr_date(text, start)):
            card = _hr_question_card(_hr_fingerprint(conversation_url, sender, text),
                                     conversation_url, sender, text, "interview_confirmation",
                                     "HR 没有明确确认同一日期和开始时间；请人工核对。")
            return json.dumps({"status": "AWAITING_HUMAN", "card": card}, ensure_ascii=False)
        if start <= datetime.now().astimezone():
            raise ValueError("拟定面试时间已过；请重新协商。")
        job = manager.get_job(str(proposal.get("job_id") or "")) or {}
        title = f"面试 · {job.get('company') or sender} · {job.get('title') or '岗位'}"
        description = f"HR：{sender}\n会话：{conversation_url}\n面试提议：{proposal_message_fingerprint}"
        with SCHEDULE_LOCK:
            existing = next((item for item in _load_schedules().get("fixed_schedules", [])
                             if item.get("owner") == "user" and
                             f"面试提议：{proposal_message_fingerprint}" in str(item.get("description") or "")
                             and item.get("status") != "cancelled"), None)
            if existing:
                result = f"面试日程已存在（ID: {existing.get('id')}），未重复创建。"
            else:
                if user_calendar_conflicts(start, end):
                    card = _hr_question_card(_hr_fingerprint(conversation_url, sender, text),
                                             conversation_url, sender, text, "interview_confirmation",
                                             "确认时个人日历已有冲突；没有自动写入面试。")
                    return json.dumps({"status": "AWAITING_HUMAN", "card": card}, ensure_ascii=False)
                result = add_schedule("fixed", title, start.isoformat(sep=" ", timespec="minutes"),
                                      "user", description=description,
                                      end_time=end.isoformat(sep=" ", timespec="minutes"))
        notice = send_user_request(
            title="面试时间已确认",
            content=(f"{title}\n时间：{start:%Y-%m-%d %H:%M}—{end:%H:%M}\n"
                     f"HR 确认：{confirmation_message}\n已写入我的日程。"),
            options=["acknowledge"], action_type="INTERVIEW_CONFIRMED",
            payload={"proposal_fingerprint": proposal_message_fingerprint,
                     "conversation_url": conversation_url},
            resume_task_on_response=False,
            idempotency_key="interview-confirmed:" + str(proposal_message_fingerprint),
        )
        return json.dumps({"status": "SCHEDULED", "schedule": result, "notice": notice}, ensure_ascii=False)


def auto_reply_hr_message(
    conversation_url: str,
    sender: str,
    message_text: str,
    sender_selector: str,
    message_selector: str,
    incoming_message_selector: str,
    reply_input_selector: str,
    send_selector: str,
    confirmation_selector: str,
    confirmation_text: str,
    conversation_selector: str = "",
) -> str:
    """Send the exact approved text once, with durable ambiguity handling.

    The caller must inspect the current WebBridge page and supply observed CSS
    selectors. The function never navigates or guesses a site-specific selector.
    OBSERVED_SENT means that a post-click page marker was seen in this session;
    it does not claim that the remote service durably accepted the message.
    """
    conversation_url = str(conversation_url or "").strip()
    sender = str(sender or "").strip()
    message_text = str(message_text or "").strip()
    confirmation_text = _hr_text(confirmation_text)
    conversation_selector = str(conversation_selector or "").strip()
    selectors = (conversation_selector, sender_selector, message_selector, incoming_message_selector,
                 reply_input_selector, send_selector, confirmation_selector)
    if (urlsplit(conversation_url).scheme.lower() not in {"http", "https"} or
            not urlsplit(conversation_url).hostname):
        raise ValueError("请提供当前会话的完整 http(s) URL。")
    if not sender or not message_text:
        raise ValueError("HR 发件人和原文不能为空。")
    if len(confirmation_text) < 3:
        raise ValueError("请提供至少 3 字符的页面发送结果标记。")
    if any(not isinstance(value, str) or not value.strip() or value.startswith("@") for value in selectors):
        raise ValueError("请提供当前会话主容器及六个已观察到的 CSS 选择器；@e 引用不可用于精确核验。")
    if conversation_selector.casefold() in {"body", "html", ":root", "main", "[role=main]", "#app", "#root"}:
        raise ValueError("会话主容器选择器必须指向当前聊天面板，不能覆盖整页或通用主区域。")

    fingerprint = _hr_fingerprint(conversation_url, sender, message_text)
    context = get_current_task()
    schedule_id = str(context.get("schedule_id") or "")
    run_job_id = str(context.get("job_id") or "")
    manager = JobRecordManager()
    base = {
        "message_fingerprint": fingerprint,
        "conversation_url": conversation_url,
        "conversation_identity": _hr_conversation_identity(conversation_url),
        "sender": sender,
        "message_text": message_text,
        "schedule_id": schedule_id,
        "run_job_id": run_job_id,
    }
    # Match the lock order used by submit_job_application: recruitment storage,
    # then the browser session. This serializes page verification and the click.
    with _JOBS_LOCK, _SESSION_LOCK:
        page_url = str(_page_info().get("url") or "")
        if not _same_hr_conversation_url(page_url, conversation_url):
            raise ValueError("当前 WebBridge 页面不是指定的 HR 会话；停止发送。")
        _hr_observe_page(conversation_url, sender, message_text,
                         str(sender_selector), str(message_selector), str(incoming_message_selector),
                         str(reply_input_selector), str(send_selector), conversation_selector)

        rows = _hr_rows(manager)
        matching_prior = [
            (key, row) for key, row in rows.items()
            if key == fingerprint or (
                _hr_conversation_identity(row.get("conversation_url")) == base["conversation_identity"]
                and _hr_text(row.get("sender")) == _hr_text(sender)
                and _hr_text(row.get("message_text")) == _hr_text(message_text)
            )
        ]
        # Records written before URL normalization retain their original key.
        # Reuse a prior attempt so a tracking URL change cannot send it twice.
        matching_prior.sort(key=lambda item: item[1].get("status") in _HR_BLOCKED_STATES, reverse=True)
        prior = matching_prior[0][1] if matching_prior else {}
        if matching_prior:
            fingerprint = matching_prior[0][0]
            base["message_fingerprint"] = fingerprint
        if prior.get("status") in _HR_BLOCKED_STATES:
            if prior["status"] in {"ATTEMPTING", "UNKNOWN"}:
                raise HRReplyNotConfirmedError(
                    f"HR 消息 {fingerprint} 的状态为 {prior['status']}；"
                    "必须核对平台会话，不能自动重试。"
                )
            return json.dumps({"message_fingerprint": fingerprint,
                               "status": prior["status"],
                               "reason": "该消息已有发送尝试或已观察到发送结果，不能自动重试。"},
                              ensure_ascii=False)
        policy, reason = _hr_rule_for_run(schedule_id, run_job_id,
                                          conversation_url, message_text, rows)
        if policy is None:
            row = _hr_store(manager, fingerprint, {
                **base, "status": "NOT_SENT", "error": reason, "reminder_sent": False,
            })
            reminder = _hr_reminder(fingerprint, conversation_url, sender, message_text, reason)
            row = _hr_store(manager, fingerprint, {"reminder_sent": True})
            row["reminder"] = reminder
            return json.dumps(row, ensure_ascii=False, sort_keys=True)

        reply_mode = str(policy.get("reply_mode") or "exact")
        guided = reply_mode == "guided"
        drafted = reply_mode == "draft"
        prepared = (prior if prior.get("status") in ({"PREPARED", "REVIEW_PENDING"} if drafted
                                                     else {"PREPARED"}) else {})
        if (guided or drafted) and (not prepared or prepared.get("conversation_url") != conversation_url
                                   or prepared.get("sender") != sender or prepared.get("message_text") != message_text):
            raise ValueError("当前 HR 原文没有已准备且匹配的回复草稿。")
        reply_text = str(prepared.get("reply_text") if guided or drafted else policy.get("reply_text") or "")
        if not reply_text.strip():
            raise ValueError("批准卡缺少精确回复文本；停止发送。")
        if guided:
            _guided_reply_still_valid(prepared)
        if drafted:
            _draft_ready_for_send(prepared, policy, fingerprint, schedule_id)
        if _hr_text(confirmation_text) in _hr_text(reply_text):
            raise ValueError("发送结果标记不能只是批准的回复文本；请选择站点的结果提示。")
        if _hr_confirmation_present(confirmation_selector, confirmation_text, conversation_url):
            row = _hr_store(manager, fingerprint, {
                **base, "status": "UNKNOWN", "reply_text": reply_text,
                "error": "发送前页面已显示结果标记，无法归因于本条消息。",
                "evidence": {"stage": "before_click", "confirmation_text": confirmation_text},
            })
            raise HRReplyNotConfirmedError(
                f"HR 消息 {fingerprint} 状态 UNKNOWN：发送前已有结果标记；"
                "请核对平台会话，不能自动重试。"
            )

        try:
            if _hr_observe_input(reply_input_selector).strip():
                raise ValueError("当前会话已有未发送的回复草稿；不得覆盖。")
            web_input(reply_input_selector, reply_text)
            if _hr_observe_input(reply_input_selector).replace("\r\n", "\n") != reply_text.replace("\r\n", "\n"):
                raise ValueError("输入框内容与批准的精确回复文本不一致。")
            _hr_observe_page(conversation_url, sender, message_text,
                             str(sender_selector), str(message_selector), str(incoming_message_selector),
                             str(reply_input_selector), str(send_selector), conversation_selector)
            _hr_observe_click_target(send_selector, conversation_url, conversation_selector)
            current_policy, current_reason = _hr_rule_for_run(
                schedule_id, run_job_id, conversation_url, message_text, _hr_rows(manager))
            if current_policy != policy:
                raise ValueError(current_reason or "HR 回复授权在发送前发生变化。")
            if guided:
                _guided_reply_still_valid(prepared)
            if drafted:
                _draft_ready_for_send(prepared, policy, fingerprint, schedule_id)
        except Exception as exc:
            row = _hr_store(manager, fingerprint, {
                **base, "status": "PRE_CLICK_FAILED", "reply_text": reply_text,
                "error": f"发送按钮未点击：{exc}",
                "evidence": {"stage": "before_click"},
            })
            return json.dumps(row, ensure_ascii=False, sort_keys=True)

        _hr_store(manager, fingerprint, {
            **base, "status": "ATTEMPTING", "reply_text": reply_text,
            "attempted_at": _hr_now(), "error": None,
            "evidence": {"stage": "before_click", "page_url": conversation_url},
        })
        try:
            _web_click_unchecked(send_selector)
            if not _hr_confirmation_present(confirmation_selector, confirmation_text, conversation_url):
                raise ValueError("发送后未在当前会话观察到结果标记。")
            row = _hr_store(manager, fingerprint, {
                "status": "OBSERVED_SENT", "observed_at": _hr_now(), "error": None,
                "evidence": {"stage": "after_click", "page_url": conversation_url,
                             "confirmation_text": confirmation_text,
                             "confirmation_selector": confirmation_selector},
            })
        except Exception as exc:
            row = _hr_store(manager, fingerprint, {
                "status": "UNKNOWN", "error": f"发送动作可能已发生，页面结果无法确认：{exc}",
                "evidence": {"stage": "after_click", "page_url": conversation_url,
                             "confirmation_text": confirmation_text},
            })
            raise HRReplyNotConfirmedError(
                f"HR 消息 {fingerprint} 状态 UNKNOWN：发送结果未确认；"
                "请核对平台会话，不能自动重试。"
            ) from exc
        return json.dumps(row, ensure_ascii=False, sort_keys=True)
