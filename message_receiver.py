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

    timestamp = headers.get("x-sl
