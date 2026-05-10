import json
import os
import re
import urllib.request
import urllib.error
import base64

# ── 환경변수 ──────────────────────────────────────────
GITHUB_TOKEN      = os.environ.get("GH_TOKEN_FINE_GRAINED", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "your-id/your-repo")
GITHUB_BRANCH     = os.environ.get("GITHUB_BRANCH", "main")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# 횟수를 기록할 파일 경로 (레포 안에 있어야 함)
COUNTER_FILE = "worker_receive_count.txt"


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
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
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


def get_file(filepath: str) -> tuple[str, str]:
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
        payload["sha"] = sha  # 기존 파일 업데이트 시 필요

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
            # 파일이 없으면 1로 시작
            count = 1
        else:
            # "워커에서 메세지를 수신했습니다. (N회)" 에서 N 추출
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
