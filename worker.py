import json
import os
import re
import urllib.request
import urllib.error
import base64
import boto3

# ── 환경변수 ──────────────────────────────────────────
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "your-id/your-repo")
GITHUB_BRANCH     = os.environ.get("GITHUB_BRANCH", "main")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# AWS Secrets Manager secret 이름
GITHUB_SECRET_NAME = "pocket-dev-agent-github-token-fine-grained-pocket-dev-agent"

# 횟수를 기록할 파일 경로
COUNTER_FILE = "worker_receive_count.txt"

# Lambda 실행 중 토큰 캐싱 (cold start 시에만 Secrets Manager 호출)
_github_token_cache: str | None = None


def get_github_token() -> str:
    """AWS Secrets Manager에서 GitHub 토큰을 가져옴. 캐싱 적용."""
    global _github_token_cache
    if _github_token_cache:
        print("[INFO] Using cached GitHub token")
        return _github_token_cache

    print(f"[INFO] Fetching secret: {GITHUB_SECRET_NAME}")
    client = boto3.client("secretsmanager", region_name="ap-northeast-2")
    response = client.get_secret_value(SecretId=GITHUB_SECRET_NAME)

    # Secrets Manager는 문자열 또는 JSON으로 저장 가능
    # 문자열로 저장했으면 그대로, JSON이면 파싱
    secret = response["SecretString"]
    try:
        parsed = json.loads(secret)
        # JSON인 경우 키 이름 후보들 시도
        token = (
            parsed.get("token")
            or parsed.get("github_token")
            or parsed.get("GH_TOKEN_FINE_GRAINED")
            or list(parsed.values())[0]  # 키 이름 무관하게 첫 번째 값
        )
    except (json.JSONDecodeError, IndexError):
        # 순수 문자열로 저장된 경우
        token = secret.strip()

    _github_token_cache = token
    print("[INFO] GitHub token fetched from Secrets Manager")
    return token


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


def github_request(method: str, path: str, data: dict = None) -> dict:
    """GitHub REST API 호출"""
    token = get_github_token()
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "pocket-dev-agent",
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API error {e.code}: {e.read().decode()}")


def get_file(filepath: str) -> tuple[str | None, str | None]:
    """GitHub에서 파일 내용과 sha 반환. 파일 없으면 (None, None) 반환"""
    try:
        data = github_request("GET", f"/repos/{GITHUB_REPO}/contents/{filepath}?ref={GITHUB_BRANCH}")
        content = base64.b64decode(data["content"]).decode("utf-8")
        sha = data["sha"]
        print(f"[INFO] Read file: {filepath} (sha={sha[:7]})")
        return content, sha
    except RuntimeError as e:
        if "404" in str(e):
            print(f"[INFO] File not found, will create: {filepath}")
            return None, None
        raise


def commit_file(filepath: str, new_content: str, sha: str | None, commit_message: str) -> str:
    """GitHub에 파일 커밋 (sha=None이면 신규 생성). 커밋 URL 반환"""
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": commit_message,
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    result = github_request("PUT", f"/repos/{GITHUB_REPO}/contents/{filepath}", payload)
    commit_url = result["commit"]["html_url"]
    print(f"[INFO] Committed: {commit_url}")
    return commit_url


def lambda_handler(event: dict, context) -> None:
    """
    Worker Lambda 진입점.
    GitHub의 카운터 파일을 읽어 횟수를 +1하고 커밋.
    """
    print(f"[INFO] Event received: {json.dumps(event)}")

    try:
        # 1. GitHub에서 카운터 파일 읽기
        content, sha = get_file(COUNTER_FILE)

        if content is None:
            count = 1
        else:
            match = re.search(r"\((\d+)회\)", content)
            count = int(match.group(1)) + 1 if match else 1

        # 2. 새 내용 구성
        new_content = f"워커에서 메세지를 수신했습니다. ({count}회)\n"
        print(f"[INFO] Updating counter to {count}")

        # 3. GitHub에 커밋
        commit_url = commit_file(
            COUNTER_FILE,
            new_content,
            sha,
            f"test: 워커 수신 횟수 {count}회 기록"
        )

        # 4. Slack에 결과 알림
        slack_webhook_post(
            f"🔧 워커에서 메세지를 수신했습니다. ({count}회)\n"
            f"커밋: {commit_url}"
        )

    except Exception as e:
        print(f"[ERROR] {e}")
        slack_webhook_post(f"❌ 워커 오류\n```{str(e)}```")
