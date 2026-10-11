"""QQ Mail tools backed by the existing Kimi WebBridge session.

Reading stays on the visible inbox page. Sending requires an exact draft review
card and records an attempt before the final click; ambiguous outcomes never
trigger an automatic retry.
"""

from __future__ import annotations

import json
import re
import hashlib
import time
from urllib.parse import urlsplit

from src.core.tasks.task_context import get_current_task
from src.tools.activity_tool import read_cards, mutate_cards, send_user_request
from src.tools import browser_atomic_tools as browser_state

from src.tools.browser_atomic_tools import (
    WebBridgeError,
    _SESSION_LOCK,
    _command,
    _evaluate_json,
    _is_qq_mail_compose,
    _js_literal,
    _page_info,
    web_use_open_tab,
)


_ADDRESS_RE = re.compile(r"^[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+$")
_COMPOSE_ROOT = ".mail-compose-draft"
_TO_FIELD = '.mail-compose-draft input[aria-label="收件人"]'
_SUBJECT_FIELD = '.mail-compose-draft input[aria-label="主题"]'
_BODY_FIELD = '.mail-compose-draft [aria-label="输入正文"][contenteditable="true"]'
_SEND_BUTTON = '.mail-compose-draft .ui-toolbar-ellipsis-btns .xmail-ui-btn.ui-btn-them-blue-lighten'


def qq_mail_open_compose() -> str:
    """Reuse the user's QQ Mail tab and open a fresh compose view, without sending."""
    with _SESSION_LOCK:
        web_use_open_tab("https://mail.qq.com")
        page = _page_info()
        parts = urlsplit(page.get("url") or "")
        if parts.scheme.lower() != "https" or parts.hostname not in {"mail.qq.com", "wx.mail.qq.com"}:
            raise WebBridgeError("WebBridge 没有选中已登录的 QQ 邮箱页面。")
        if not _is_qq_mail_compose(page["url"]):
            code = """(() => {
              const controls = [...document.querySelectorAll('.frame-sidebar-compose-btn')]
                .filter(el => el.getClientRects().length &&
                  getComputedStyle(el).visibility !== 'hidden');
              return JSON.stringify({count: controls.length,
                label: controls.length === 1 ? String(controls[0].innerText || '').trim() : ''});
            })()"""
            observed = _evaluate_json(code)
            if (not isinstance(observed, dict) or observed.get("count") != 1 or
                    "写信" not in str(observed.get("label") or "")):
                raise WebBridgeError("当前 QQ 邮箱页面没有唯一的写信入口；请手动打开空白写信页。")
            _command("click", {"selector": ".frame-sidebar-compose-btn"})
        state = _wait_compose_state()
        if (state["toValue"].strip() or state["recipientAddresses"] or
                state["subjectValue"].strip() or state["bodyText"].strip()):
            raise ValueError("当前写信页已有内容；未覆盖已有邮件，请手动打开新的空白写信页。")
        return json.dumps({"status": "COMPOSE_OPEN", "sent": False,
                           "next_step": "可调用 qq_mail_write_draft 填写空白写信页。"}, ensure_ascii=False)


def _compose_state() -> dict:
    """Read only the current compose form; values stay inside this call."""
    code = """(() => {
      const roots = [...document.querySelectorAll(__ROOT__)];
      const root = roots.length === 1 ? roots[0] : null;
      const visible = el => Boolean(el && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden');
      const find = selector => root ? [...root.querySelectorAll(selector)].filter(visible) : [];
      const to = find('input[aria-label="收件人"]');
      const subject = find('input[aria-label="主题"]');
      const body = find('[aria-label="输入正文"][contenteditable="true"]');
      const editor = body.length === 1 ? body[0] : null;
      const signature = editor ? editor.querySelector('xm-signature') : null;
      const ownBody = editor ? [...editor.childNodes]
        .filter(node => node !== signature)
        .map(node => node.nodeType === 1 ? node.innerText || node.textContent || '' : node.textContent || '')
        .join('\\n').trim() : '';
      const recipientArea = root?.querySelector('.mail-compose-receivers');
      const recipientText = String(recipientArea?.innerText || '');
      const addresses = [...new Set(recipientText.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/gi) || [])];
      return JSON.stringify({
        url: String(location.href || ''), roots: roots.length,
        toCount: to.length, subjectCount: subject.length, bodyCount: body.length,
        toValue: to.length === 1 ? to[0].value : '',
        subjectValue: subject.length === 1 ? subject[0].value : '',
        bodyText: ownBody, recipientAddresses: addresses,
        hasSignature: Boolean(signature), signatureHtml: signature?.outerHTML || '',
        signatureText: String(signature?.innerText || '').trim(),
        firstBodyTag: editor?.firstElementChild?.tagName || '',
        senderText: String(root?.querySelector('.mail-compose-footer .xmail-cmp-sender-selector')?.innerText || '').trim(),
        attachedFileCount: [...(root?.querySelectorAll('input[type="file"]') || [])]
          .reduce((count, input) => count + (input.files?.length || 0), 0),
        attachmentItemCount: root?.querySelectorAll(
          '[class*="attachment-item"], [class*="attach-item"], [class*="file-item"]')?.length || 0
      });
    })()""".replace("__ROOT__", _js_literal(_COMPOSE_ROOT))
    state = _evaluate_json(code)
    if not isinstance(state, dict) or not _is_qq_mail_compose(str(state.get("url") or "")):
        raise WebBridgeError("当前页面不是 QQ 邮箱写信页；请先在已登录的邮箱中打开写信。")
    if any(state.get(key) != 1 for key in ("roots", "toCount", "subjectCount", "bodyCount")):
        raise WebBridgeError("QQ 邮箱写信表单结构与已观察到的版本不符，已停止填写。")
    return state


def _wait_compose_state() -> dict:
    """A newly opened QQ compose form renders asynchronously."""
    deadline = time.monotonic() + 3.0
    while True:
        try:
            return _compose_state()
        except WebBridgeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _insert_body_preserving_signature(body: str) -> None:
    code = """(() => {
      const editor = document.querySelector(__EDITOR__);
      if (!editor || editor.getClientRects().length === 0) return JSON.stringify({ok:false});
      const signature = editor.querySelector('xm-signature');
      const first = editor.firstElementChild;
      if (!first || first === signature || first.tagName !== 'DIV' ||
          String(first.innerText || '').trim()) return JSON.stringify({ok:false});
      editor.focus();
      const range = document.createRange();
      range.selectNodeContents(first);
      range.collapse(true);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      const applied = document.execCommand('insertText', false, __BODY__);
      return JSON.stringify({ok:applied || String(first.innerText || '').includes(__BODY__)});
    })()""".replace("__EDITOR__", _js_literal(_BODY_FIELD)).replace("__BODY__", _js_literal(body))
    result = _evaluate_json(code)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise WebBridgeError("QQ 邮箱正文编辑器没有确认写入内容；请检查页面，勿自动重试。")


def qq_mail_write_draft(subject: str, body: str, recipient_email: str = "") -> str:
    """Fill one fresh QQ Mail compose page without sending or replacing a draft.

    The user or Agent opens the compose page first. Recipient is optional so the
    Agent can draft wording before the address is known. The result confirms
    visible fields only; QQ Mail's server-side draft persistence is unverified.
    """
    subject = str(subject or "").strip()
    body = str(body or "").replace("\r\n", "\n").strip()
    recipient_email = str(recipient_email or "").strip()
    if not subject or len(subject) > 500 or "\n" in subject or "\r" in subject:
        raise ValueError("邮件主题须为 1 到 500 字的单行文本。")
    if not body or len(body) > 12000 or "\x00" in body:
        raise ValueError("邮件正文须为 1 到 12000 字的文本。")
    if recipient_email and not _ADDRESS_RE.fullmatch(recipient_email):
        raise ValueError("收件人须是一个完整邮箱地址；地址未知时请留空。")

    with _SESSION_LOCK:
        page = _page_info()
        if not _is_qq_mail_compose(page.get("url", "")):
            raise ValueError("请先复用已登录的 QQ 邮箱标签页并打开空白写信页。")
        before = _wait_compose_state()
        if (before["toValue"].strip() or before["recipientAddresses"] or
                before["subjectValue"].strip() or before["bodyText"].strip()):
            raise ValueError("当前写信页已有内容；为避免覆盖，先打开新的空白邮件。")
        if before["firstBodyTag"] != "DIV":
            raise WebBridgeError("QQ 邮箱正文编辑器结构已变化，未填写草稿。")

        try:
            _insert_body_preserving_signature(body)
            _command("fill", {"selector": _SUBJECT_FIELD, "value": subject})
            if recipient_email:
                _command("fill", {"selector": _TO_FIELD, "value": recipient_email})
                # Trigger the site's normal recipient validation; a raw input
                # value is not yet a confirmed recipient chip.
                _command("click", {"selector": _SUBJECT_FIELD})
            after = _compose_state()
        except Exception as exc:
            raise WebBridgeError(
                "写信页可能已填入部分内容，请在浏览器检查后手动处理，勿自动重试。"
                f"失败类型：{type(exc).__name__}。"
            ) from exc

        expected_body = " ".join(body.split())
        actual_body = " ".join(str(after["bodyText"]).split())
        recipient_seen = (not recipient_email or
                          recipient_email.casefold() == str(after["toValue"]).strip().casefold() or
                          recipient_email.casefold() in {
                              str(address).casefold() for address in after["recipientAddresses"]
                          })
        if (after["subjectValue"] != subject or actual_body != expected_body or
                after["signatureHtml"] != before["signatureHtml"] or not recipient_seen):
            raise WebBridgeError("写信页内容或签名未通过填写后核对；请在浏览器手动检查，勿自动重试。")
        return json.dumps({
            "status": "FILLED_ON_PAGE",
            "recipient_status": ("CONFIRMED" if recipient_email.casefold() in
                                 {str(address).casefold() for address in after["recipientAddresses"]}
                                 else "VISIBLE_NOT_CONFIRMED") if recipient_email else "NOT_SET",
            "subject_length": len(subject), "body_length": len(body),
            "signature_preserved": True,
            "saved_to_drafts": "UNVERIFIED", "sent": False,
            "next_step": "请在 QQ 邮箱写信页检查内容。若用户明确要求寄出，下一步调用 qq_mail_prepare_send 创建确认卡。",
        }, ensure_ascii=False)


def _mail_host(url: str) -> bool:
    parts = urlsplit(str(url or ""))
    return parts.scheme.lower() == "https" and parts.hostname in {"mail.qq.com", "wx.mail.qq.com"}


def _inbox_page(url: str) -> bool:
    return _mail_host(url) and urlsplit(url).fragment.startswith("/list/1")


def qq_mail_list_unread(limit: int = 20) -> str:
    """List unread sender, subject, time and preview from the visible inbox page.

    The tool does not open or mark individual messages as read. It may open one
    new QQ Mail tab to preserve a user's in-progress compose page.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是 1 到 30 的整数。") from exc
    if not 1 <= limit <= 30:
        raise ValueError("limit 必须是 1 到 30 的整数。")
    with _SESSION_LOCK:
        try:
            page = _page_info()
        except WebBridgeError:
            page = {}
        if not _inbox_page(page.get("url", "")):
            try:
                web_use_open_tab("https://mail.qq.com")
                page = _page_info()
            except WebBridgeError:
                page = {}
        if not _inbox_page(page.get("url", "")):
            # Preserve a compose page, including drafts the user may be editing.
            _command("navigate", {"url": "https://mail.qq.com", "newTab": True})
            browser_state._CURRENT_TAB_BORROWED = False
            page = _page_info()
        if not _inbox_page(page.get("url", "")):
            raise WebBridgeError("未进入 QQ 邮箱收件箱；没有读取邮件列表。")
        code = """(() => {
          const rows = [...document.querySelectorAll('.mail-list-page-item')];
          const visible = el => Boolean(el && el.getClientRects().length &&
            getComputedStyle(el).visibility !== 'hidden');
          const clean = (el, max) => String(el?.innerText || el?.textContent || '').trim().slice(0, max);
          const unread = rows.filter(row => visible(row) && row.querySelector('.mail-unread'));
          return JSON.stringify({route:String(location.hash || ''), visibleRows:rows.filter(visible).length,
            unreadOnPage:unread.length, messages:unread.slice(0, __LIMIT__).map(row => ({
              sender:clean(row.querySelector('.senders-list'),120),
              subject:clean(row.querySelector('.mail-subject'),200),
              time:clean(row.querySelector('.mail-time'),80),
              preview:clean(row.querySelector('.mail-digest'),300)
            }))});
        })()""".replace("__LIMIT__", str(limit))
        deadline = time.monotonic() + 3.0
        result = _evaluate_json(code)
        while (isinstance(result, dict) and not result.get("visibleRows") and
               time.monotonic() < deadline):
            time.sleep(0.3)
            result = _evaluate_json(code)
        if not isinstance(result, dict) or not str(result.get("route") or "").startswith("#/list/1"):
            raise WebBridgeError("QQ 邮箱收件箱结构已变化；未确认未读邮件列表。")
        if not result.get("visibleRows"):
            return json.dumps({"status": "UNKNOWN_PAGE_EMPTY", "visible_rows": 0,
                               "unread_on_page": None, "messages": [],
                               "note": "收件箱列表未渲染，不能据此判断没有新邮件。"}, ensure_ascii=False)
        return json.dumps({
            "status": "VISIBLE_UNREAD_LIST", "visible_rows": result.get("visibleRows", 0),
            "unread_on_page": result.get("unreadOnPage", 0),
            "messages": result.get("messages", []),
            "note": "仅当前收件箱页面可见邮件；不代表全部文件夹或所有分页。",
        }, ensure_ascii=False)


def _sendable_state() -> tuple[dict, str]:
    state = _wait_compose_state()
    addresses = [str(x).strip().casefold() for x in state["recipientAddresses"]]
    if (len(addresses) != 1 or state["toValue"].strip() or
            not _ADDRESS_RE.fullmatch(addresses[0]) or
            not state["subjectValue"].strip() or not state["bodyText"].strip() or
            state["attachedFileCount"] or state["attachmentItemCount"]):
        raise ValueError("邮件必须有唯一、已确认的收件人、主题和正文，且不能带附件；未准备发送。")
    if not state["senderText"]:
        raise WebBridgeError("当前 QQ 邮箱页面没有显示发件账户；未准备发送。")
    payload = {"sender": state["senderText"], "recipient": addresses[0],
               "subject": state["subjectValue"], "body": state["bodyText"],
               "signature": state["signatureHtml"]}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return state, digest


def qq_mail_prepare_send() -> str:
    """Create an exact-content approval card for the current filled compose page."""
    context = get_current_task()
    if context.get("source") != "manual" or not context.get("task_id"):
        raise PermissionError("邮件发送审批只能从用户当前对话发起。")
    with _SESSION_LOCK:
        state, digest = _sendable_state()
        return send_user_request(
            title="确认发送 QQ 邮件",
            content=(f"发件账户：{state['senderText']}\n收件人：{state['recipientAddresses'][0]}\n"
                     f"主题：{state['subjectValue']}\n\n正文：\n{state['bodyText']}\n\n"
                     f"签名：\n{state['signatureText'] or '（无）'}\n\n"
                     "确认后才会尝试发送。请核对以上内容。"),
            options=["approve", "reject"], action_type="QQ_MAIL_SEND",
            payload={"draft_hash": digest, "send_state": "PREPARED"},
            idempotency_key=f"qq-mail-send:{context['task_id']}:{digest}",
        )


def qq_mail_send_approved(card_id: str) -> str:
    """Send the unchanged, approved draft once; never retry an uncertain click."""
    card_id = str(card_id or "").strip()
    context = get_current_task()
    if context.get("source") != "approval" or card_id not in set(context.get("approval_card_ids") or []):
        raise PermissionError("只能在该邮件确认卡批准后的任务续办中发送。")
    with _SESSION_LOCK:
        card = next((x for x in read_cards() if x.get("card_id") == card_id), None)
        if (not card or card.get("action_type") != "QQ_MAIL_SEND" or
                card.get("status") != "approved" or
                str((card.get("workflow") or {}).get("task_id") or "") != str(context.get("task_id") or "")):
            raise PermissionError("邮件确认卡不存在、未批准或与本次任务不匹配。")
        send_state = str((card.get("payload") or {}).get("send_state") or "")
        if send_state != "PREPARED":
            if send_state == "SENT_VISIBLE":
                return json.dumps({"status": send_state, "sent": True,
                                   "note": "此前页面已显示发送成功；未重复点击。"}, ensure_ascii=False)
            raise WebBridgeError("该确认卡已有发送尝试但结果不明；未重复点击，请核对已发送文件夹。")
        state, digest = _sendable_state()
        if digest != (card.get("payload") or {}).get("draft_hash"):
            raise ValueError("写信页的账户、收件人、主题、正文或签名与审批时不同；未发送。")
        inspected = _evaluate_json("""(() => {
          const nodes=[...document.querySelectorAll(__SELECTOR__)];
          const visible=e=>Boolean(e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
          const matching=nodes.filter(e=>visible(e) && !e.classList.contains('ui-btn-disabled') &&
            String(e.innerText || '').trim()==='发送');
          return JSON.stringify({count:matching.length});
        })()""".replace("__SELECTOR__", _js_literal(_SEND_BUTTON)))
        if not isinstance(inspected, dict) or inspected.get("count") != 1:
            raise WebBridgeError("当前写信页没有唯一可用的发送按钮；未点击。")

        def mark_attempt(cards):
            current = next((x for x in cards if x.get("card_id") == card_id), None)
            if not current or (current.get("payload") or {}).get("send_state") != "PREPARED":
                return False
            current["payload"]["send_state"] = "ATTEMPTING"
            return True
        if not mutate_cards(mark_attempt):
            raise RuntimeError("发送状态已变化；没有重复点击。")
        try:
            _command("click", {"selector": _SEND_BUTTON})
            result_status = "UNKNOWN"
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                observation = _evaluate_json("""(() => {
                  const candidates=[...document.querySelectorAll('[role="alert"], [class*="toast"], [class*="notification"]')];
                  const visible=candidates.filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
                  return JSON.stringify({success:visible.some(e=>/发送成功|邮件已发送/.test(String(e.innerText || '')))});
                })()""")
                if isinstance(observation, dict) and observation.get("success") is True:
                    result_status = "SENT_VISIBLE"
                    break
                time.sleep(0.3)
        except Exception:
            result_status = "UNKNOWN"

        def mark_result(cards):
            current = next((x for x in cards if x.get("card_id") == card_id), None)
            if current and (current.get("payload") or {}).get("send_state") == "ATTEMPTING":
                current["payload"]["send_state"] = result_status
        mutate_cards(mark_result)
        if result_status != "SENT_VISIBLE":
            raise WebBridgeError("QQ 邮件发送结果无法确认；请核对已发送文件夹，不能自动重试。")
        return json.dumps({
            "status": result_status, "sent": True,
            "note": "QQ 邮箱页面显示发送成功；这不证明收件人已收到。",
        }, ensure_ascii=False)
