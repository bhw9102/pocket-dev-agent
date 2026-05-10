import json
import os
import hashlib
import hmac
import time
import urllib.request

# ── 환경변수 ──────────────────────────────────────────
WORKER_FUNCTION_NAME = os.environ.get("WORKER_FUNCTION_NAME", "pocket-deve-agent-worker")
SLACK_WEBHOOK_URL    = os.environ.get("SLACK_WEBHOOK_URL", "")


def verify_slack_signature(headers: dict, body: str) -> bool:
    """
    Slack 요청이 진짜인지 서명 검증.
    SLACK_SIGNING_SECRET 환경변수 필요.
    """
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        print("[WARN] SLACK_SIGNING_SECRET not set, skipping verification")
        return True

    timestamp = headers.get("x-slack-request-timestamp", "")
    slack_signature = headers.get("x-slack-signature", "")

    if not timestamp or not slack_signature:
        print("[WARN] Missing timestamp or signature headers")
        return False

    # 5분 이상 지난 요청은 리플레이 공격으로 간주
    try:
        if abs(time.time() - int(timestamp)) > 300:
            print("[WARN] Request timestamp too old")
            return False
    except ValueError:
        print("[WARN] Invalid timestamp format")
        return False

    sig_basestring = f"v0:{timestamp}:{body}"
    computed = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(computed, slack_signature)


def slack_webhook_post(text: str) -> None:
    """Webhook URL로 Slack에 메시지 전송"""
    if not SLACK_WEBHOOK_URL:
        print("[WARN] SLACK_WEBHOOK_URL not set")
        return

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        print(f"[INFO] Slack webhook response: {resp.status}")


# def invoke_worker_async(payload: dict) -> None:
#     """
#     Worker Lambda를 비동기(Event) 방식으로 호출.
#     ※ 테스트 중 비활성화
#     """
#     import boto3
#     client = boto3.client("lambda")
#     client.invoke(
#         FunctionName=WORKER_FUNCTION_NAME,
#         InvocationType="Event",
#         Payload=json.dumps(payload).encode("utf-8"),
#     )
#     print(f"[INFO] Worker invoked async: {WORKER_FUNCTION_NAME}")


def lambda_handler(event: dict, context) -> dict:
    """
    Receiver Lambda 진입점.

    현재 모드: 테스트
      1. Slack 서명 검증
      2. URL Verification 챌린지 처리
      3. app_mention 수신 시 Webhook으로 직접 응답 (Worker 호출 비활성화)
    """
    # --- HTTP body 파싱 ---
    raw_body = event.get("body", "") or ""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    print(f"[INFO] Headers: {list(headers.keys())}")
    print(f"[INFO] Body: {raw_body[:200]}")

    # --- Slack 서명 검증 ---
    if not verify_slack_signature(headers, raw_body):
        print("[ERROR] Slack signature verification failed")
        return {"statusCode": 403, "body": "Forbidden"}

    print("[INFO] Signature verified OK")

    # --- JSON 파싱 ---
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        print("[ERROR] Invalid JSON body")
        return {"statusCode": 400, "body": "Bad Request"}

    slack_event_type = body.get("type")

    # --- URL Verification ---
    if slack_event_type == "url_verification":
        challenge = body.get("challenge", "")
        print(f"[INFO] URL verification challenge: {challenge}")
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain"},
            "body": challenge,
        }

    # --- 일반 이벤트 처리 ---
    if slack_event_type == "event_callback":
        inner_event = body.get("event", {})
        event_type = inner_event.get("type", "")

        # bot 자신의 메시지 무시 (무한루프 방지)
        if inner_event.get("bot_id"):
            print("[INFO] Ignoring bot message")
            return {"statusCode": 200, "body": "OK"}

        if event_type == "app_mention":
            text = inner_event.get("text", "")
            event_id = body.get("event_id", "")
            print(f"[INFO] app_mention received, event_id={event_id}, text={text}")

            # ── 테스트: Worker 대신 Webhook으로 직접 응답 ──
            slack_webhook_post(f"✅ Receiver 정상 동작 확인!\n수신 메시지: {text}")

            # ── 운영 전환 시 아래 주석 해제 + 위 webhook_post 제거 ──
            # worker_payload = {
            #     "event_id": event_id,
            #     "channel": inner_event.get("channel", ""),
            #     "user": inner_event.get("user", ""),
            #     "text": text,
            #     "ts": inner_event.get("ts", ""),
            # }
            # invoke_worker_async(worker_payload)

        else:
            print(f"[INFO] Unhandled event type: {event_type}")

    # --- Slack에 즉시 200 반환 ---
    return {"statusCode": 200, "body": "OK"}
