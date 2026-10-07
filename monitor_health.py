"""Read scheduled workflow results; persist alert transitions independently."""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BRANCH = "monitor-health-state"
PATH = "health.json"
STALE = 26 * 3600


def request(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
        headers={"User-Agent": "DiscordMonitorHealth/1.0", "Content-Type": "application/json", **(headers or {})}, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def health(runs, now, started, enabled=True):
    if not enabled:
        return "disabled"
    # Manual connection tests cannot count as a healthy scheduled monitor run.
    scheduled = sorted((r for r in runs if r.get("event") == "schedule"), key=lambda r: r["created_at"], reverse=True)
    completed = [r for r in scheduled if r["status"] == "completed"]
    successful = [r for r in completed if r["conclusion"] == "success"]
    last = int(datetime.fromisoformat(successful[0]["updated_at"].replace("Z", "+00:00")).timestamp()) if successful else started
    if now - last > STALE:
        return "stale"
    if len(completed) >= 2 and all(r["conclusion"] != "success" for r in completed[:2]):
        return "failed"
    if completed and completed[0]["conclusion"] == "success":
        return "ok"
    return "waiting"


def transition(old, current):
    if current == "waiting":
        return None
    if current == "ok":
        return "recovered" if old in ("failed", "stale", "disabled", "unavailable") else None
    return current if old != current else None


def upgrade_delivery(state):
    """Keep v1 workflow history; never guess which legacy batch messages arrived."""
    if state.get("delivery") == "unconfirmed":
        state.setdefault("legacy_unconfirmed", {"checked": state.get("checked"), "workflows": json.loads(json.dumps(state["workflows"]))})
    state.pop("delivery", None)
    for key in ("delivery_queue", "delivery_pending", "delivery_receipts"):
        state.setdefault(key, {})
        if not isinstance(state[key], dict):
            raise ValueError("Invalid health delivery history")
    state.setdefault("delivery_sequence", 0)
    if not isinstance(state["delivery_sequence"], int) or state["delivery_sequence"] < 0:
        raise ValueError("Invalid health event sequence")


def enqueue(state, component, change, text, now):
    state["delivery_sequence"] += 1
    identity = str(state["delivery_sequence"]) + ":" + component + ":" + change
    state["delivery_queue"][identity] = {"component": component, "change": change, "text": text, "at": now}
    return identity


def deliver_events(state, save, send):
    """Persist reservation before POST; only explicit rejections are retryable."""
    blocked = {r["component"] for r in state["delivery_pending"].values()}
    errors = []
    for identity, event in list(state["delivery_queue"].items()):
        if identity in state["delivery_receipts"]:
            state["delivery_queue"].pop(identity)
            save()
            continue
        if event["component"] in blocked:
            continue
        state["delivery_pending"][identity] = dict(event)
        save()  # Failure here must prevent POST.
        try:
            result = send(event["text"])
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError) and error.code in (400, 401, 403, 404, 405, 413, 429):
                state["delivery_pending"].pop(identity)
                save()
            blocked.add(event["component"])
            errors.append(type(error).__name__)
            continue
        if not isinstance(result, dict) or not result.get("id"):
            blocked.add(event["component"])
            errors.append("UnconfirmedReceipt")
            continue
        state["delivery_receipts"][identity] = {**event, "message_id": str(result["id"])}
        state["delivery_pending"].pop(identity)
        state["delivery_queue"].pop(identity)
        save()
    return errors


def main():
    api = os.environ["GITHUB_API_URL"] + "/repos/" + os.environ["GITHUB_REPOSITORY"]
    headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json"}
    webhook = os.environ["DISCORD_WEBHOOK_URL"].strip()
    user = os.environ["DISCORD_MENTION_USER_ID"].strip()
    if not re.fullmatch(r"[0-9]{17,20}", user):
        raise RuntimeError("Mention User ID is missing or invalid")
    p = urllib.parse.urlsplit(webhook)
    if p.scheme != "https" or p.netloc not in ("discord.com", "discordapp.com") or not re.fullmatch(r"/api/webhooks/[^/]+/[^/]+", p.path) or p.query or p.fragment:
        raise RuntimeError("Monitor Webhook Secret is missing or invalid")

    def send_notice(chunk):
        result = request(urllib.parse.urlunsplit(p._replace(query="wait=true")),
            {"content": "<@" + user + ">\n" + chunk, "allowed_mentions": {"parse": [], "users": [user]}})
        if not result.get("id") or not any(str(u.get("id")) == user for u in result.get("mentions", [])):
            raise RuntimeError("Discord mention receipt unconfirmed; check channel before retrying")
        return result

    if os.environ.get("TEST_MENTION", "false").lower() == "true":
        send_notice("【接続テスト】監視状態通知の本人メンション確認です。定期監視の状態は変更していません。")
        print("Mention test receipt confirmed.")
        return

    def gh(path, data=None, method=None):
        return request(api + path, data, headers, method)

    try:
        gh("/git/ref/heads/" + BRANCH)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        gh("/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": os.environ["GITHUB_SHA"]})
    sha = None
    now = int(time.time())
    try:
        saved = gh("/contents/" + PATH + "?ref=" + BRANCH)
        sha = saved["sha"]
        state = json.loads(base64.b64decode(saved["content"]))
        if state.get("version") != 1 or not isinstance(state.get("workflows"), dict):
            raise RuntimeError("Invalid health history")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        if gh("/commits?sha=" + BRANCH + "&path=" + PATH + "&per_page=1"):
            raise RuntimeError("Health history was deleted; restore it before running")
        state = {"version": 1, "workflows": {}, "checked": now}

    def save():
        nonlocal sha
        data = {"branch": BRANCH, "message": "Update monitor health [skip ci]",
            "content": base64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()}
        if sha:
            data["sha"] = sha
        sha = gh("/contents/" + PATH, data, "PUT")["content"]["sha"]

    delivery_migration = "delivery_queue" not in state or "delivery" in state
    upgrade_delivery(state)
    original_records = json.dumps({k: v.get("status") for k, v in state["workflows"].items()}, sort_keys=True)
    workflows = []
    for page in range(1, 11):
        found = gh("/actions/workflows?per_page=100&page=" + str(page))["workflows"]
        workflows.extend(found)
        if len(found) < 100:
            break
    else:
        raise RuntimeError("Too many workflows")
    changes = []
    for workflow in workflows:
        path = workflow["path"]
        if not path.startswith(".github/workflows/") or path.endswith("monitor-health.yml"):
            continue
        try:
            content = gh("/contents/" + urllib.parse.quote(path, safe="/") + "?ref=" + urllib.parse.quote(os.environ["DEFAULT_BRANCH"], safe=""))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue  # Deleted workflows are no longer configured monitors.
            raise
        source = base64.b64decode(content["content"]).decode()
        if not re.search(r"^\s+schedule\s*:", source, re.M):
            continue
        key = str(workflow["id"])
        record = state["workflows"].setdefault(key, {"started": now, "status": "ok"})
        runs = gh("/actions/workflows/" + key + "/runs?event=schedule&per_page=10")["workflow_runs"]
        current = health(runs, now, record["started"], workflow["state"] == "active")
        if current == "stale":
            # A stale filtered listing alone cannot establish an outage.
            # Cross-check the independent repository listing before alerting.
            recent = gh("/actions/runs?per_page=100")["workflow_runs"]
            corroborating = [r for r in recent if str(r.get("workflow_id")) == key and r.get("event") == "schedule"]
            combined = {str(r["id"]): r for r in runs + corroborating}
            runs = list(combined.values())
            current = health(runs, now, record["started"], workflow["state"] == "active")
        completed = sorted((r for r in runs if r.get("event") == "schedule" and r.get("status") == "completed"), key=lambda r: r["created_at"], reverse=True)
        evidence = {"checked_at": now, "result": current, "run_count": len(runs),
            "latest_completed": {k: completed[0].get(k) for k in ("id", "updated_at", "conclusion")} if completed else None}
        record["evidence"] = evidence
        print(json.dumps({"workflow_id": key, **evidence}, sort_keys=True))
        change = transition(record["status"], current)
        if change:
            changes.append((workflow, change))
            record["status"] = current
        elif current == "ok":
            record["status"] = "ok"
    # Independent probe: this Actions job runs outside Cloudflare and the PC.
    cloud_key = "cloud-notification-service"
    cloud_record = state["workflows"].setdefault(cloud_key, {"started": now, "status": "ok"})
    try:
        probe = request("https://haru-work-notifications.haruru23432.workers.dev/healthz")
        cloud_status = "ok" if probe == {"ok": True} else "unavailable"
    except (urllib.error.URLError, TimeoutError, ValueError):
        cloud_status = "unavailable"
    cloud_change = transition(cloud_record["status"], cloud_status)
    cloud_record["status"] = cloud_status
    cloud_record["evidence"] = {"checked_at": now, "result": cloud_status}
    print(json.dumps({"component": cloud_key, **cloud_record["evidence"]}, sort_keys=True))
    if cloud_change:
        changes.append(({"name": "Work通知基盤（対象Work自体の停止・完了を意味しません）", "id": cloud_key,
            "url": "https://haru-work-notifications.haruru23432.workers.dev/healthz"}, cloud_change))
    if changes:
        # Queue each transition independently before reserving any actual POST.
        labels = {"failed": "⚠️ 定期監視が2回連続で失敗しました", "stale": "⚠️ 26時間以上、定期監視の正常完了を確認できません",
            "unavailable": "⚠️ クラウド通知基盤の稼働を確認できません",
            "disabled": "⚠️ 定期監視が無効になっています", "recovered": "✅ 定期監視が復旧しました"}
        for workflow, change in changes:
            name = workflow["name"][:150]
            link = workflow.get("url") or os.environ["GITHUB_SERVER_URL"] + "/" + os.environ["GITHUB_REPOSITORY"] + "/actions/workflows/" + str(workflow["id"])
            enqueue(state, str(workflow["id"]), change, labels[change] + "\n" + name + "\n" + link, now)
        state["checked"] = now
        save()
    elif delivery_migration or now - state["checked"] >= 7 * 86400 or sha is None or original_records != json.dumps({k: v.get("status") for k, v in state["workflows"].items()}, sort_keys=True):
        state["checked"] = now
        save()
    deliver_events(state, save, send_notice)
    if state["delivery_pending"] or state.get("legacy_unconfirmed") or state["delivery_queue"]:
        raise RuntimeError("Health alert delivery unresolved; inspect durable event records")
    print("Health check complete; unchanged states are silent.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("::error::点検処理を完了できませんでした（" + type(error).__name__ + "）。Actionsの履歴・Secretsを確認してください。")
        raise SystemExit(1)
