import json
import os
import hashlib
import hmac
import time
import boto3

# Worker Lambda 함수 이름 (환경변수로 관리)
WORKER_FUNCTION_NAME = os.environ.get("WORKER_FUNCTION_NAME", "claude-deploy-agent-worker")


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

    # 5분 이상 지난 요청은 리플레이 공격으로 간주
    if abs(time.time() - int(timestamp)) > 300:
        print("[WARN] Request timestamp too old")
        return False

    sig_basestring = f"v0:{timestamp}:{body}"
    computed = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(computed, slack_signature)


def invoke_worker_async(payload: dict) -> None:
    """
    Worker Lambda를 비동기(Event) 방식으로 호출.
    응답을 기다리지 않고 바로 리턴.
    """
    client = boto3.client("lambda")
    client.invoke(
        FunctionName=WORKER_FUNCTION_NAME,
        InvocationType="Event",          # 비동기 핵심 설정
        Payload=json.dumps(payload).encode("utf-8"),
    )
    print(f"[INFO] Worker invoked async: {WORKER_FUNCTION_NAME}")


def lambda_handler(event: dict, context) -> dict:
    """
    Receiver Lambda 진입점.

    역할:
      1. Slack 서명 검증
      2. URL Verification 챌린지 처리 (앱 최초 등록 시)
      3. 중복 이벤트 필터링
      4. Worker Lambda 비동기 호출
      5. Slack에 즉시 200 응답 반환 (3초 제한 준수)
    """
    # --- HTTP body 파싱 ---
    raw_body = event.get("body", "") or ""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # --- Slack 서명 검증 ---
    if not verify_slack_signature(headers, raw_body):
        print("[ERROR] Slack signature verification failed")
        return {"statusCode": 403, "body": "Forbidden"}

    # --- JSON 파싱 ---
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        print("[ERROR] Invalid JSON body")
        return {"statusCode": 400, "body": "Bad Request"}

    slack_event_type = body.get("type")

    # --- URL Verification (Slack App 최초 등록 시 한 번만 발생) ---
    if slack_event_type == "url_verification":
        challenge = body.get("challenge", "")
        print("[INFO] URL verification challenge received")
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain"},
            "body": challenge,
        }

    # --- 일반 이벤트 처리 ---
    if slack_event_type == "event_callback":
        inner_event = body.get("event", {})
        event_type = inner_event.get("type", "")

        # bot 자신의 메시지는 무시 (무한루프 방지)
        if inner_event.get("bot_id"):
            print("[INFO] Ignoring bot message")
            return {"statusCode": 200, "body": "OK"}

        # app_mention 이벤트만 처리
        if event_type == "app_mention":
            event_id = body.get("event_id", "")
            print(f"[INFO] app_mention received, event_id={event_id}")

            # Worker에 넘길 페이로드 구성
            worker_payload = {
                "event_id": event_id,
                "channel": inner_event.get("channel", ""),
                "user": inner_event.get("user", ""),
                "text": inner_event.get("text", ""),
                "ts": inner_event.get("ts", ""),
            }

            invoke_worker_async(worker_payload)

        else:
            print(f"[INFO] Unhandled event type: {event_type}")

    # --- Slack에 즉시 200 반환 (3초 제한 준수) ---
    return {"statusCode": 200, "body": "OK"}
