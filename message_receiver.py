import json
import os
import hashlib
import hmac
import time
import urllib.request
import boto3
from botocore.exceptions import ClientError

# ── 환경변수 ──────────────────────────────────────────
WORKER_FUNCTION_NAME = os.environ.get("WORKER_FUNCTION_NAME", "pocket-deve-agent-worker")
SLACK_WEBHOOK_URL    = os.environ.get("SLACK_WEBHOOK_URL", "")
DEDUP_TABLE_NAME     = os.environ.get("DEDUP_TABLE_NAME", "slack-event-dedup")

# ── DynamoDB ──────────────────────────────────────────
dynamodb = boto3.resource("dynamodb", region_name="ap-northeast-2")


def is_duplicate_event(event_id: str) -> bool:
    """event_id를 DynamoDB에 기록. 이미 있으면 True(중복) 반환"""
    table = dynamodb.Table(DEDUP_TABLE_NAME)
    try:
        table.put_item(
            Item={
                "event_id": event_id,
                "ttl": int(time.time()) + 300  # 5분 후 자동 삭제
            },
            ConditionExpression="attribute_not_exists(event_id)"
        )
        return False  # 신규 이벤트
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"[INFO] Duplicate event detected: {event_id}")
            return True  # 중복 이벤트
        raise


def verify_slack_signature(headers: dict, body: str) -> bool:
    """Slack 요청 서명 검증"""
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        print("[WARN] SLACK_SIGNING_SECRET not set, skipping verification")
        return True

    timestamp = headers.get("x-slack-request-timestamp", "")
    slack_signature = headers.get("x-slack-signature", "")

    if not timestamp or not slack_signature:
        print("[WARN] Missing timestamp or signature headers")
        return False

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


def invoke_worker_async(payload: dict) -> None:
    """Worker Lambda를 비동기(Event) 방식으로 호출"""
    client = boto3.client("lambda")
    client.invoke(
        FunctionName=WORKER_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    print(f"[INFO] Worker invoked async: {WORKER_FUNCTION_NAME}")


def lambda_handler(event: dict, context) -> dict:
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

            # 1. 중복 이벤트 체크
            if is_duplicate_event(event_id):
                print(f"[INFO] Skipping duplicate event: {event_id}")
                return {"statusCode": 200, "body": "OK"}

            # 2. 수신 알림 발송
            slack_webhook_post("📨 리시버에서 메세지를 수신했습니다.")

            # 3. Worker 비동기 호출
            worker_payload = {
                "event_id": event_id,
                "channel": inner_event.get("channel", ""),
                "user": inner_event.get("user", ""),
                "text": text,
                "ts": inner_event.get("ts", ""),
            }
            invoke_worker_async(worker_payload)

        else:
            print(f"[INFO] Unhandled event type: {event_type}")

    # --- Slack에 즉시 200 반환 ---
    return {"statusCode": 200, "body": "OK"}
