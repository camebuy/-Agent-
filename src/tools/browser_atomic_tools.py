"""Browser tools backed by the Kimi Browser Extension local-agent bridge.

The bridge speaks HTTP on localhost and controls the browser session managed by
the installed extension. This module never launches a browser or connects over
Chrome DevTools Protocol.
"""
import http.client
import json
import re
import threading
import time
from string import Template
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

from config.settings import settings
from src.tools.database_tool import get_file_path


_SESSION_LOCK = threading.RLock()
_CURRENT_TAB_BORROWED = False


class WebBridgeError(RuntimeError):
    """Raised when Kimi WebBridge cannot confirm a browser operation."""


def _bridge_endpoint() -> str:
    return f"http://{settings.KIMI_WEBBRIDGE_HOST}:{settings.KIMI_WEBBRIDGE_PORT}"


def _command(action: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Send one command to the configured Kimi WebBridge session.

    Browser actions are not automatically replayed: a timed-out click may have
    happened already, so callers should inspect the page before retrying.
    """
    payload = json.dumps(
        {
            "action": action,
            "args": args or {},
            "session": settings.KIMI_WEBBRIDGE_SESSION,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    connection = None
    try:
        with _SESSION_LOCK:
            connection = http.client.HTTPConnection(
                settings.KIMI_WEBBRIDGE_HOST,
                settings.KIMI_WEBBRIDGE_PORT,
                timeout=settings.KIMI_WEBBRIDGE_TIMEOUT,
            )
            connection.request(
                "POST",
                "/command",
                body=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            response = connection.getresponse()
            raw_response = response.read()
            status = response.status
    except (OSError, http.client.HTTPException) as exc:
        raise WebBridgeError(
            f"Cannot confirm Kimi WebBridge action '{action}' at "
            f"{_bridge_endpoint()}: {exc}. Confirm the extension and its local "
            "agent connection are active; inspect the page before repeating an action."
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    try:
        result = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebBridgeError(
            f"Kimi WebBridge returned an invalid response for '{action}' "
            f"(HTTP {status})."
        ) from exc

    if status < 200 or status >= 300:
        raise WebBridgeError(
            f"Kimi WebBridge action '{action}' returned HTTP {status}: "
            f"{_error_message(result)}"
        )
    if not isinstance(result, dict):
        raise WebBridgeError(f"Kimi WebBridge returned an unexpected response for '{action}'.")
    if result.get("ok") is not True:
        raise WebBridgeError(
            f"Kimi WebBridge returned no confirmed success envelope for '{action}': {_error_message(result)}"
        )
    data = result.get("data")
    if result.get("success") is False or (isinstance(data, dict) and data.get("success") is False):
        raise WebBridgeError(
            f"Kimi WebBridge action '{action}' failed: {_error_message(data if isinstance(data, dict) else result)}"
        )
    return result


def _error_message(result: Any) -> str:
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)
        if error:
            return str(error)
        return str(result.get("message") or result)
    return str(result)


def _evaluate(code: str) -> Any:
    result = _command("evaluate", {"code": code})
    data = result.get("data")
    if not isinstance(data, dict) or "value" not in data:
        raise WebBridgeError(
            "Kimi WebBridge did not return an evaluated page value. "
            "The session may not have an active tab."
        )
    return data["value"]


def _evaluate_json(code: str) -> Any:
    value = _evaluate(code)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _js_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _page_info() -> Dict[str, str]:
    value = _evaluate_json(
        "JSON.stringify({url: String(location.href || ''), "
        "title: String(document.title || '')})"
    )
    if not isinstance(value, dict):
        raise WebBridgeError("Kimi WebBridge could not read the current page details.")
    return {
        "url": str(value.get("url") or ""),
        "title": str(value.get("title") or ""),
    }


def _is_boss_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "zhipin.com" or host.endswith(".zhipin.com")


def _is_qq_mail_compose(url: str) -> bool:
    parts = urlsplit(str(url or ""))
    return (parts.scheme.lower() == "https" and
            parts.hostname in {"mail.qq.com", "wx.mail.qq.com"} and
            parts.fragment.startswith("/compose"))


def _is_boss_homepage(url: str) -> bool:
    parts = urlsplit(url)
    return _is_boss_url(url) and parts.path.rstrip("/") == "" and not parts.query


_ACTION_TOKEN = re.compile(
    r"(?:^|[/_.-])(?:submit(?:application|resume|job)?|apply(?:now|job)?|"
    r"deliver(?:resume)?|send(?:message|msg|resume)?|postmessage|投递|应聘|发送|提交)(?:$|[/_.-])",
    re.I,
)
_ACTION_VALUE = re.compile(
    r"^(?:submit(?:application|resume|job)?|apply(?:now|job)?|"
    r"deliver(?:resume)?|send(?:message|msg|resume)?|postmessage|投递|应聘|发送|提交)$",
    re.I,
)


def _risky_action_url(url: str) -> bool:
    """Reject URLs that look like GET endpoints for an irreversible action."""
    parts = urlsplit(str(url or ""))
    path = unquote(parts.path)
    fragment = unquote(parts.fragment)
    if _ACTION_TOKEN.search(path) or _ACTION_TOKEN.search(fragment):
        return True
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key = unquote(key).casefold()
        value = unquote(value).strip()
        if _ACTION_VALUE.fullmatch(key):
            return True
        if key in {"action", "operation", "op", "do", "method", "event", "mode", "type"} and _ACTION_TOKEN.search("/" + value):
            return True
    return False


def web_navigate(url: str) -> str:
    """Navigate the configured Kimi WebBridge session without launching a browser."""
    global _CURRENT_TAB_BORROWED
    requested = str(url or "").strip()
    if not requested:
        raise ValueError("url is required")
    target_url = requested if urlsplit(requested).scheme else f"https://{requested}"
    if urlsplit(target_url).scheme.lower() not in ("http", "https") or not urlsplit(target_url).hostname:
        raise ValueError("Only http and https URLs are supported.")
    if _risky_action_url(target_url):
        raise PermissionError("此 URL 含疑似投递或发送动作；请使用相应的专用工具及审批流程。")

    with _SESSION_LOCK:
        try:
            current = _page_info()
        except WebBridgeError:
            current = {}

        current_url = current.get("url", "")
        if _is_boss_homepage(current_url) and _is_boss_homepage(target_url):
            return (
                f"BOSS is already open in the Kimi WebBridge session at {current_url}; "
                "the current page was preserved."
            )
        if current_url.rstrip("/") == target_url.rstrip("/"):
            return f"Already at {current_url}; the current page was preserved."

        # A borrowed user tab is left intact when the task switches websites.
        # Within the same site, normal navigation continues in that tab.
        current_host = (urlsplit(current_url).hostname or "").lower().removeprefix("www.")
        target_host = (urlsplit(target_url).hostname or "").lower().removeprefix("www.")
        new_tab = bool(_CURRENT_TAB_BORROWED and current_host and current_host != target_host)
        _command("navigate", {"url": target_url, "newTab": new_tab})
        if new_tab:
            _CURRENT_TAB_BORROWED = False
        try:
            page = _page_info()
        except WebBridgeError as exc:
            raise WebBridgeError(
                f"Kimi WebBridge accepted navigation to {target_url}, but the final "
                f"page state is not verified yet: {exc}"
            ) from exc

        page_url = page.get("url", "")
        if urlsplit(page_url).scheme.lower() not in ("http", "https"):
            raise WebBridgeError(
                f"Kimi WebBridge accepted navigation to {target_url}, "
                f"but the session is blank or blocked ({page_url})."
            )
        if _risky_action_url(page_url):
            raise WebBridgeError(
                f"Navigation ended at a possible action URL ({page_url}); do not repeat this navigation. "
                "Inspect the page and any platform record before continuing."
            )
        return (
            f"Navigation reached {page.get('title') or 'page'} ({page_url}) "
            "through Kimi WebBridge."
        )


def web_use_open_tab(url: str, active: bool = True) -> str:
    """Select an existing WebBridge tab for a host without navigating it.

    With active=True, find_tab may borrow the user's currently viewed tab.
    A bare host and its www-prefixed form are treated as the same host.
    """
    global _CURRENT_TAB_BORROWED
    requested = str(url or "").strip()
    if not requested:
        raise ValueError("url is required")
    if not isinstance(active, bool):
        raise ValueError("active must be a boolean")
    target_url = requested if urlsplit(requested).scheme else f"https://{requested}"
    target_parts = urlsplit(target_url)
    if target_parts.scheme.lower() not in ("http", "https") or not target_parts.hostname:
        raise ValueError("Only http and https URLs are supported.")

    with _SESSION_LOCK:
        try:
            selection = _command("find_tab", {"url": target_url, "active": active})
        except WebBridgeError as exc:
            if not active:
                raise WebBridgeError(f"Could not select an already-open tab for {target_url}: {exc}") from exc
            try:
                selection = _command("find_tab", {"url": target_url, "active": False})
            except WebBridgeError as fallback_exc:
                raise WebBridgeError(
                    f"Could not select the user's active tab or a session tab for {target_url}: "
                    f"{exc}; {fallback_exc}"
                ) from fallback_exc
        data = selection.get("data")
        if not isinstance(data, dict) or data.get("success") is not True or not data.get("tabId"):
            raise WebBridgeError(f"Kimi WebBridge did not confirm an open tab for {target_url}.")
        try:
            page = _page_info()
        except WebBridgeError as exc:
            raise WebBridgeError(f"Could not verify the selected open tab for {target_url}: {exc}") from exc
        page_url = page.get("url", "")
        page_parts = urlsplit(page_url)
        requested_host = target_parts.hostname.lower().rstrip(".").removeprefix("www.")
        actual_host = (page_parts.hostname or "").lower().rstrip(".").removeprefix("www.")
        same_host = (actual_host == requested_host or
                     (requested_host == "mail.qq.com" and actual_host == "wx.mail.qq.com"))
        if page_parts.scheme.lower() not in ("http", "https") or not same_host:
            _CURRENT_TAB_BORROWED = False
            raise WebBridgeError(
                f"Selected tab is not on the requested host {target_parts.hostname}: "
                f"{page_url or 'empty URL'}. The tab was not navigated."
            )
        _CURRENT_TAB_BORROWED = data.get("borrowed") is True
        origin = "user's active tab" if _CURRENT_TAB_BORROWED else "session tab"
        return f"Using already-open {origin} {page.get('title') or 'page'} ({page_url}) through Kimi WebBridge."


def _web_click_unchecked(selector: str) -> str:
    """Physical click used by tools that already enforced a business authorization."""
    selector = str(selector or "").strip()
    if not selector:
        raise ValueError("selector is required")
    with _SESSION_LOCK:
        result = _command("click", {"selector": selector})
    data = result.get("data")
    if isinstance(data, dict) and data.get("success") is False:
        raise WebBridgeError(
            f"Kimi WebBridge could not click '{selector}': {_error_message(data)}"
        )
    return f"Kimi WebBridge clicked {selector}."


def _is_recruitment_host(url: str) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    known = ("zhipin.com", "liepin.com", "zhaopin.com", "51job.com", "lagou.com")
    if any(host == site or host.endswith("." + site) for site in known):
        return True
    # A user-approved policy may target another recruitment platform. The
    # generic browser click must not bypass its dedicated send tool either.
    from src.tools.activity_tool import read_cards
    for card in read_cards():
        if card.get("action_type") not in {"SCHEDULE_AUTO_APPLY", "SCHEDULE_HR_AUTO_REPLY", "JOB_SUBMISSION"}:
            continue
        payload = card.get("payload") or {}
        approved = str(payload.get("site_host") or "").lower()
        if not approved:
            approved = (urlsplit(str(payload.get("job_url") or "")).hostname or "").lower()
        if approved and (host == approved or host.endswith("." + approved)):
            return True
    return False


def _page_looks_recruitment(expected_url: str = "") -> bool:
    """Recognize an unfamiliar careers site from its current visible page."""
    code = """(() => {
      const url = String(location.href || '');
      const host = String(location.hostname || '').toLowerCase();
      const path = String(location.pathname || '').toLowerCase();
      const title = String(document.title || '').toLowerCase();
      const text = String(document.body ? document.body.innerText : '').slice(0, 50000).toLowerCase();
      const hostHint = /(?:^|[.-])(?:jobs?|careers?|hiring|recruit(?:ment)?|zhipin)(?:[.-]|$)/.test(host)
        || /workdayjobs|greenhouse|lever\\.co/.test(host);
      const pathHint = path.split('/').some(part =>
        /^(?:jobs?|careers?|recruit(?:ment)?|hiring|招聘|职位|岗位)$/.test(part));
      const titleHint = /招聘|求职|职位|岗位|job openings|careers|recruitment|hiring/.test(title);
      const applicationHint = /application|apply for/.test(title)
        && /resume|简历|岗位|职位|job|position/.test(text);
      const signals = [/招聘|recruiter|recruitment/, /岗位|职位|job opening|position/,
        /简历|resume|curriculum vitae/, /投递|apply for|application/, /薪资|salary/,
        /学历|工作经验|experience required|work location/]
        .filter(pattern => pattern.test(text)).length;
      return JSON.stringify({url, looksRecruitment:
        hostHint || pathHint || titleHint || applicationHint || signals >= 3});
    })()"""
    observed = _evaluate_json(code)
    if not isinstance(observed, dict) or not str(observed.get("url") or ""):
        raise WebBridgeError("无法核验当前页面是否属于招聘流程。")
    if expected_url and str(observed["url"]) != expected_url:
        raise WebBridgeError("当前网页在点击检查期间发生变化；请重新观察页面。")
    return _is_recruitment_host(str(observed["url"])) or observed.get("looksRecruitment") is True


_FINAL_CLICK_RE = re.compile(
    r"投递|应聘|申请|(?:立即|发起|开始|去)?沟通|联系\s*(?:HR|招聘|面试|recruiter)|"
    r"聊聊|发起对话|开始聊天|"
    r"发送|回复|提交|\b(?:apply|submit|deliver|send|reply)\b|"
    r"contact\s+(?:hr|recruiter)|start\s+(?:chat|conversation)",
    re.I,
)
_READ_ONLY_LABEL_RE = re.compile(
    r"^(?:我的)?(?:投递|申请|沟通)(?:记录|历史|进度|状态|中)$|"
    r"^(?:查看|浏览)(?:投递|申请|回复|消息|沟通)(?:记录|历史|进度|状态)?$",
    re.I,
)
_CONFIRM_CLICK_RE = re.compile(r"^(?:确认|确定|完成|继续|下一步|提交|ok|confirm|finish|continue|next)$", re.I)
_SAFE_FORM_CLICK_RE = re.compile(
    r"^(?:搜索|查询|筛选|过滤|排序|重置|上一页|下一页|更多|展开|收起|"
    r"search|find|filter|sort|reset|previous|next)(?:\s.*)?$",
    re.I,
)


def _inspect_click_target(selector: str) -> Dict[str, Any]:
    """Read one visible CSS target and its clickable ancestor before acting."""
    code = Template("""(() => {
      const nodes = Array.from(document.querySelectorAll($selector));
      const visible = el => Boolean(el && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden' &&
        getComputedStyle(el).opacity !== '0' && !el.closest('[aria-hidden="true"], [inert]'));
      const shown = nodes.filter(visible);
      const selected = shown.length === 1 ? shown[0] : null;
      const target = selected && (selected.closest(
        'button, a[href], input, select, [role="button"], [role="link"], [tabindex], label'
      ) || selected);
      const form = target && target.closest('form');
      const region = target && target.closest('[role="dialog"], dialog, form');
      const rawHref = target && target.getAttribute('href');
      let href = '';
      try { href = rawHref && !/^javascript:/i.test(rawHref) ? new URL(rawHref, location.href).href : ''; }
      catch (_) { href = ''; }
      const tag = target ? target.tagName.toLowerCase() : '';
      const type = target ? String(target.getAttribute('type') || '').toLowerCase() : '';
      const label = target ? String(target.innerText || target.value ||
        target.getAttribute('aria-label') || target.getAttribute('title') ||
        target.getAttribute('alt') || selected.innerText || '').trim().slice(0, 180) : '';
      const isFormSubmit = Boolean(form && (type === 'submit' ||
        (tag === 'button' && !type)));
      return JSON.stringify({
        url: String(location.href || ''), count: shown.length,
        actionable: Boolean(target && target.matches(
          'button, a[href], input[type="button"], input[type="submit"], '
          + '[role="button"], [role="link"], [tabindex], label')),
        enabled: Boolean(target && !target.disabled &&
          target.getAttribute('aria-disabled') !== 'true' &&
          getComputedStyle(target).pointerEvents !== 'none'),
        tag, type, label, href,
        rawHref: String(rawHref || '').slice(0, 240),
        formAction: String(form ? form.action : target && target.formAction || '').slice(0, 500),
        isFormSubmit, withinDialog: Boolean(target && target.closest('[role="dialog"], dialog')),
        regionText: String(region ? region.innerText || '' : '').slice(0, 600),
        actionHint: String(target && (
          target.getAttribute('data-action') || target.getAttribute('data-event') ||
          target.getAttribute('onclick') || '') || '').slice(0, 240)
      });
    })()""").substitute(selector=_js_literal(selector))
    observed = _evaluate_json(code)
    if not isinstance(observed, dict):
        raise WebBridgeError("无法检查当前页面的点击目标。")
    return observed


def _click_looks_final(observed: Dict[str, Any], recruitment: bool) -> bool:
    label = " ".join(str(observed.get("label") or "").split())
    hint = " ".join(str(observed.get("actionHint") or "").split())
    if _FINAL_CLICK_RE.search(hint):
        return True
    if _FINAL_CLICK_RE.search(label) and not _READ_ONLY_LABEL_RE.fullmatch(label):
        return True
    if observed.get("href") and _risky_action_url(str(observed["href"])):
        return True
    if observed.get("formAction") and _risky_action_url(str(observed["formAction"])):
        return True
    if recruitment:
        region = str(observed.get("regionText") or "")
        if _CONFIRM_CLICK_RE.fullmatch(label):
            if label.casefold() not in {"next"} or observed.get("withinDialog") or observed.get("isFormSubmit"):
                return True
            if _FINAL_CLICK_RE.search(region):
                return True
        if observed.get("isFormSubmit") and not _SAFE_FORM_CLICK_RE.fullmatch(label):
            return True
        if not label and not observed.get("href"):
            return True
    return False


def web_list_controls(text_contains: str = "", limit: int = 80) -> str:
    """Inspect current DOM controls and narrow text containers without clicking.

    CSS paths are generated from elements actually present on the current page.
    The optional text search also exposes nearby container paths for exact
    receipt or conversation checks by the dedicated recruitment tools.
    """
    needle = " ".join(str(text_contains or "").split())
    if len(needle) > 160:
        raise ValueError("text_contains must be 160 characters or fewer")
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer from 1 to 120") from exc
    if limit < 1 or limit > 120:
        raise ValueError("limit must be an integer from 1 to 120")

    code = """(() => {
      const needle = __NEEDLE__;
      const limit = __LIMIT__;
      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const visible = el => Boolean(el && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden' &&
        getComputedStyle(el).opacity !== '0' && !el.closest('[aria-hidden="true"], [inert]'));
      const selectorFor = el => {
        if (!el || el.nodeType !== 1) return '';
        if (el.id) {
          const byId = '#' + CSS.escape(el.id);
          if (document.querySelectorAll(byId).length === 1) return byId;
        }
        const parts = [];
        for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
          let part = node.localName.toLowerCase();
          if (node.id) {
            const byId = '#' + CSS.escape(node.id);
            if (document.querySelectorAll(byId).length === 1) {
              parts.unshift(byId);
              return parts.join(' > ');
            }
          }
          const siblings = node.parentElement
            ? Array.from(node.parentElement.children).filter(sibling => sibling.localName === node.localName)
            : [node];
          part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
          parts.unshift(part);
          const candidate = parts.join(' > ');
          if (document.querySelectorAll(candidate).length === 1) return candidate;
        }
        return parts.join(' > ');
      };
      const controls = Array.from(document.querySelectorAll(
        'a[href], button, input:not([type="hidden"]), select, textarea, '
        + '[role="button"], [role="link"], [role="tab"], [contenteditable="true"]'
      )).filter(el => visible(el) || el.matches('input[type="file"]'));
      const wantedControl = clean(needle).toLocaleLowerCase();
      const matchedControls = wantedControl ? controls.filter(el => clean(
        el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') ||
        el.getAttribute('placeholder') ||
        (el.matches('input[type="button"], input[type="submit"]') ? el.value : '') || ''
      ).toLocaleLowerCase().includes(wantedControl)) : controls;
      const mapped = matchedControls.slice(0, limit).map(el => {
        const label = clean(el.innerText || el.getAttribute('aria-label') ||
          el.getAttribute('title') || el.getAttribute('placeholder') ||
          (el.matches('input[type="button"], input[type="submit"]') ? el.value : '')).slice(0, 140);
        let href = '';
        try { href = el.href ? new URL(el.href, location.href).href : ''; }
        catch (_) { href = ''; }
        return {css: selectorFor(el), tag: el.localName.toLowerCase(),
          role: String(el.getAttribute('role') || ''), type: String(el.getAttribute('type') || ''),
          label, href, visible: visible(el),
          formAction: String(el.formAction || el.closest('form')?.action || '').slice(0, 350)};
      });
      const textMatches = [];
      if (needle) {
        const wanted = clean(needle).toLocaleLowerCase();
        const candidates = document.querySelectorAll(
          'h1,h2,h3,h4,p,span,div,section,article,li,td,strong,button,label,'
          + '[role="button"],[role="status"],[role="alert"]'
        );
        for (let i = 0; i < candidates.length && textMatches.length < 12; i++) {
          const el = candidates[i];
          if (!String(el.textContent || '').toLocaleLowerCase().includes(wanted) ||
              !visible(el) || !clean(el.innerText).toLocaleLowerCase().includes(wanted)) continue;
          if (Array.from(el.children).some(child =>
            visible(child) && clean(child.innerText).toLocaleLowerCase().includes(wanted))) continue;
          const ancestors = [];
          for (let node = el, depth = 0; node && depth < 4; node = node.parentElement, depth++) {
            const content = clean(node.innerText);
            ancestors.push({css: selectorFor(node), text: content.slice(0, 300), length: content.length});
          }
          textMatches.push({ancestors});
        }
      }
      return JSON.stringify({url: String(location.href || ''), title: String(document.title || ''),
        totalControls: controls.length, matchedControls: matchedControls.length,
        controls: mapped, textMatches});
    })()""".replace("__NEEDLE__", _js_literal(needle)).replace("__LIMIT__", str(limit))
    with _SESSION_LOCK:
        result = _evaluate_json(code)
    if not isinstance(result, dict) or urlsplit(str(result.get("url") or "")).scheme.lower() not in ("http", "https"):
        raise WebBridgeError("Kimi WebBridge could not inspect controls on a loaded page.")
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def web_click(selector: str) -> str:
    """Click one observed benign control; leave final sends to dedicated tools.

    Final job submissions and HR sends must use the dedicated recruitment
    tools, which call _web_click_unchecked only after approval and page checks.
    """
    selector = str(selector or "").strip()
    if not selector:
        raise ValueError("selector is required")
    with _SESSION_LOCK:
        page_url = _page_info()["url"]
        if _is_qq_mail_compose(page_url):
            raise PermissionError("QQ 邮箱写信页暂不允许通用点击；请使用专用草稿工具或手动操作。")
        recruitment = _page_looks_recruitment(page_url)
        if selector.startswith("@"):
            if recruitment:
                raise PermissionError("招聘页面请先用 web_list_controls 获取唯一 CSS；最终投递或发送须使用专用工具。")
            _, tree = _read_accessibility_snapshot()
            lines = [line for line in tree.splitlines() if re.search(r"(?<![\w])" + re.escape(selector) + r"(?![\w])", line)]
            if len(lines) != 1 or _FINAL_CLICK_RE.search(lines[0]):
                raise PermissionError("无法确认 @e 引用是普通控件；请先检查页面并用唯一 CSS 选择器。")
            return _web_click_unchecked(selector)

        observed = _inspect_click_target(selector)
        if (observed.get("url") != page_url or observed.get("count") != 1 or
                observed.get("actionable") is not True or observed.get("enabled") is not True):
            raise PermissionError("点击目标必须是当前页面上唯一可见、可用的控件；请用 web_list_controls 检查真实 CSS。")
        if _click_looks_final(observed, recruitment):
            raise PermissionError("通用点击已拦截疑似最终投递、申请或消息发送；请使用相应专用工具及授权。")

        target_url = str(observed.get("href") or "")
        raw_href = str(observed.get("rawHref") or "")
        if target_url and raw_href not in {"#", ""}:
            target = urlsplit(target_url)
            if target.scheme.lower() not in ("http", "https"):
                raise PermissionError("链接目标必须是 HTTP(S) 页面。")
            if recruitment and target.hostname != urlsplit(page_url).hostname:
                raise PermissionError("招聘页面上的普通链接须指向同一网站。")
            if _risky_action_url(target_url):
                raise PermissionError("链接可能触发投递或发送动作；请使用专用工具及授权。")
            _command("navigate", {"url": target_url, "newTab": False})
            reached = _page_info()["url"]
            reached_parts = urlsplit(reached)
            if reached_parts.scheme.lower() not in ("http", "https") or reached_parts.hostname != target.hostname:
                raise WebBridgeError(f"浏览器未到达所核验链接的网站：{reached or '空白页面'}。请检查当前页面。")
            if _risky_action_url(reached):
                raise WebBridgeError(
                    f"链接重定向到疑似动作 URL ({reached})；不要重复点击，请核查页面和平台记录。"
                )
            return f"Kimi WebBridge opened an observed link; current page is {reached}."

        if raw_href and raw_href.lower().startswith("javascript:"):
            if recruitment and not _SAFE_FORM_CLICK_RE.fullmatch(str(observed.get("label") or "").strip()):
                raise PermissionError("招聘页面中的脚本链接用途不明确；请使用普通按钮或已观察到的安全链接。")
        if raw_href == "#" and recruitment:
            label = str(observed.get("label") or "").strip()
            if not (_SAFE_FORM_CLICK_RE.fullmatch(label) or label.isdigit()):
                raise PermissionError("招聘页面中的 # 链接用途不明确；请检查页面控件。")
        if recruitment and not str(observed.get("label") or "").strip():
            raise PermissionError(
                "招聘页面上的无标签控件用途不明确；请用 web_list_controls 找到明确的控件。"
            )
        return _web_click_unchecked(selector)


def web_input(selector: str, text: str) -> str:
    """Fill a field with Kimi WebBridge; accepts CSS selectors or snapshot @e references."""
    selector = str(selector or "").strip()
    if not selector:
        raise ValueError("selector is required")
    text = str(text)

    with _SESSION_LOCK:
        if _is_qq_mail_compose(_page_info()["url"]):
            raise PermissionError("QQ 邮箱写信页请使用 qq_mail_write_draft，不可用通用网页输入。")
        result = _command("fill", {"selector": selector, "value": text})

    data = result.get("data")
    if isinstance(data, dict) and data.get("success") is False:
        raise WebBridgeError(
            f"Kimi WebBridge could not fill '{selector}': {_error_message(data)}"
        )
    if result.get("success") is False:
        raise WebBridgeError(
            f"Kimi WebBridge could not fill '{selector}': {_error_message(result)}"
        )

    mode = data.get("mode") if isinstance(data, dict) else None
    detail = f" (mode: {mode})" if mode else ""
    return f"Kimi WebBridge filled the field {selector}{detail}."


def _web_upload_unchecked(selector: str, file_id: str) -> str:
    """Attach one catalogued file after a dedicated workflow checked approval.

    A file ID is required so the agent cannot name an arbitrary local path. A
    successful upload command only confirms that the browser received the file;
    submitting a form and checking the site's result are separate actions.
    """
    selector = str(selector or "").strip()
    file_id = str(file_id or "").strip()
    if not selector:
        raise ValueError("selector is required")
    if not file_id:
        raise ValueError("file_id is required")

    path = get_file_path(file_id)
    with _SESSION_LOCK:
        result = _command("upload", {"selector": selector, "files": [str(path)]})

    data = result.get("data")
    if not isinstance(data, dict) or data.get("success") is not True or data.get("fileCount") != 1:
        raise WebBridgeError(
            f"Kimi WebBridge did not confirm uploading exactly one file to '{selector}': "
            f"{_error_message(data if isinstance(data, dict) else result)}"
        )
    return (
        f"Kimi WebBridge attached catalogued file {file_id} ({path.name}) to {selector}. "
        "The website's form submission and acceptance have not been verified."
    )


def web_upload(selector: str, file_id: str) -> str:
    """Attach a catalogued file on ordinary sites; recruitment uses guarded tools."""
    with _SESSION_LOCK:
        if _is_qq_mail_compose(_page_info()["url"]):
            raise PermissionError("QQ 邮箱写信页暂不允许通用上传附件；请手动处理。")
        if _page_looks_recruitment():
            raise PermissionError(
                "招聘页面上传简历需要岗位和文件授权；请使用专用投递工具。"
            )
        return _web_upload_unchecked(selector, file_id)


def web_verify_page(expected_text: str = "", expected_url_contains: str = "") -> str:
    """Confirm observed text and/or URL on the current page after an action.

    Both supplied markers must match. This is a read-only observation of the
    current tab, not proof that a remote service persisted the action.
    """
    expected_text = " ".join(str(expected_text or "").split())
    expected_url_contains = str(expected_url_contains or "").strip()
    if not expected_text and not expected_url_contains:
        raise ValueError("Provide expected_text or expected_url_contains to verify the page.")

    with _SESSION_LOCK:
        page, tree = _read_accessibility_snapshot()
        page_url = page.get("url") or _page_info()["url"]
        page_title = page.get("title") or "Untitled"
        if urlsplit(page_url).scheme.lower() not in ("http", "https"):
            raise WebBridgeError(f"Kimi WebBridge has no loaded page to verify ({page_url or 'empty URL'}).")

        if expected_url_contains and expected_url_contains.casefold() not in page_url.casefold():
            raise WebBridgeError(
                f"Current page URL does not contain '{expected_url_contains}': {page_url}. "
                "Inspect the page before retrying the preceding action."
            )

        tree_text = " ".join(tree.split()).casefold()
        if expected_text and expected_text.casefold() not in tree_text:
            # The accessibility snapshot is capped for model context. Probe
            # visible page text directly before treating a missing marker as a
            # failed verification; do not return the page's full private text.
            marker = _js_literal(expected_text)
            code = f"""(() => {{
              const marker = {marker};
              const visible = String(document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').toLocaleLowerCase();
              return JSON.stringify({{url: String(location.href || ''), found: visible.includes(marker.toLocaleLowerCase())}});
            }})()"""
            probe = _evaluate_json(code)
            if not isinstance(probe, dict) or probe.get("url") != page_url or probe.get("found") is not True:
                raise WebBridgeError(
                    f"Current page did not show expected text '{expected_text}' at {page_url}. "
                    "Inspect the page before retrying the preceding action."
                )

    matched = []
    if expected_text:
        matched.append(f"text={expected_text!r}")
    if expected_url_contains:
        matched.append(f"url contains {expected_url_contains!r}")
    return (
        f"Current page {page_title} ({page_url}) shows {' and '.join(matched)}. "
        "This confirms the observed page state; check the site's own receipt or record "
        "before marking a submission or message as accepted."
    )


def _read_accessibility_snapshot():
    """Read the current page's Kimi WebBridge accessibility tree and metadata."""
    with _SESSION_LOCK:
        response = _command("snapshot", {})

    data = response.get("data")
    if data is None:
        data = {key: value for key, value in response.items() if key not in ("ok", "success")}

    page = {"url": "", "title": ""}
    if isinstance(data, str):
        tree = data
    elif isinstance(data, dict):
        page = {
            "url": str(data.get("url") or response.get("url") or ""),
            "title": str(data.get("title") or response.get("title") or ""),
        }
        tree = next(
            (
                data[key]
                for key in ("tree", "snapshot", "accessibility_tree", "accessibilityTree", "content")
                if key in data
            ),
            None,
        )
        if tree is None:
            tree = {key: value for key, value in data.items() if key not in ("url", "title")}
    else:
        tree = data

    if isinstance(tree, str):
        rendered_tree = tree.strip()
    else:
        rendered_tree = json.dumps(tree, ensure_ascii=False, separators=(",", ":")).strip()

    if not rendered_tree:
        rendered_tree = "[Kimi WebBridge returned an empty accessibility tree]"
    if len(rendered_tree) > 10000:
        rendered_tree = rendered_tree[:10000] + "\n[Snapshot truncated at 10000 characters.]"
    return page, rendered_tree


def web_snapshot() -> str:
    """Inspect the current page through Kimi WebBridge's accessibility snapshot."""
    page, tree = _read_accessibility_snapshot()
    title = page.get("title") or "Untitled"
    url = page.get("url") or "unknown URL"
    return (
        f"Current page: {title} ({url})\n"
        "Accessibility snapshot (use the actual @e references and labels shown here):\n"
        f"{tree}"
    )


def _snapshot_after_selector_problem(selector: str, reason: str) -> str:
    snapshot = web_snapshot()
    return (
        f"{reason} The page snapshot is included below. Do not retry guessed selectors; "
        "choose content or element references that actually appear in this snapshot.\n"
        f"{snapshot}"
    )


def web_extract(selector: Optional[str] = None) -> str:
    """Extract page text; without a selector, return the Kimi accessibility snapshot."""
    selector = str(selector).strip() if selector else ""
    if not selector:
        return web_snapshot()
    if selector.startswith("@"):
        return _snapshot_after_selector_problem(
            selector, "Accessibility references are used with click/fill, not document.querySelector.",
        )

    selector_js = _js_literal(selector)
    code = f"""(() => {{
      const selector = {selector_js};
      const element = document.querySelector(selector);
      return JSON.stringify({{
        url: String(location.href || ""),
        title: String(document.title || ""),
        found: Boolean(element),
        text: element ? String(element.innerText || element.textContent || "") : ""
      }});
    }})()"""
    try:
        with _SESSION_LOCK:
            result = _evaluate_json(code)
    except WebBridgeError as exc:
        return _snapshot_after_selector_problem(
            selector,
            f"CSS extraction for '{selector}' failed: {exc}.",
        )

    if not isinstance(result, dict):
        return _snapshot_after_selector_problem(
            selector,
            f"Kimi WebBridge returned an unreadable result for selector '{selector}'.",
        )
    if not result.get("found"):
        return _snapshot_after_selector_problem(
            selector,
            f"No page element matched CSS selector '{selector}'.",
        )

    text = " ".join(str(result.get("text") or "").split())[:8000]
    if not text:
        return _snapshot_after_selector_problem(
            selector,
            f"Selector '{selector}' matched an element, but it contained no visible text.",
        )
    return (
        f"Current page: {result.get('title') or 'Untitled'} ({result.get('url') or 'unknown URL'})\n"
        f"Extracted text for the observed selector '{selector}':\n{text}"
    )
def web_scroll(direction: str = "down") -> str:
    """Scroll the current page by 800 pixels through Kimi WebBridge."""
    direction = str(direction or "down").strip().lower()
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    distance = 800 if direction == "down" else -800
    code = f"(() => {{ window.scrollBy(0, {distance}); return window.scrollY; }})()"
    with _SESSION_LOCK:
        scroll_y = _evaluate(code)
    return f"Scrolled {direction}; current scroll position is {scroll_y}."


def web_wait(seconds: int = 3) -> str:
    """Wait for the current WebBridge page to render, then verify its URL."""
    try:
        wait_seconds = max(0, min(int(seconds), 30))
    except (TypeError, ValueError) as exc:
        raise ValueError("seconds must be an integer from 0 to 30") from exc

    with _SESSION_LOCK:
        time.sleep(wait_seconds)
        page_url = str(_evaluate("String(location.href || '')") or "")
    if urlsplit(page_url).scheme.lower() not in ("http", "https"):
        raise WebBridgeError("Kimi WebBridge has no loaded page to wait on.")
    return f"Waited {wait_seconds} seconds on {page_url}."
