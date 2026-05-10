import json
import os
import re
import urllib.request
import urllib.error
import base64

# ── 환경변수 ──────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GH_TOKEN_FINE_GRAINED", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "your-id/your-repo")
GITHUB_BRANCH     = os.environ.get("GITHUB_BRANCH", "main")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


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
    """GitHub에서 파일 내용과 sha 반환"""
    data = github_request("GET", f"/repos/{GITHUB_REPO}/contents/{filepath}?ref={GITHUB_BRANCH}")
    content = base64.b64decode(data["content"]).decode("utf-8")
    sha = data["sha"]
    print(f"[INFO] Read file: {filepath} (sha={sha[:7]})")
    return content, sha


def commit_file(filepath: str, new_content: str, sha: str, commit_message: str) -> str:
    """GitHub에 파일 커밋. 커밋 URL 반환"""
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
    result = github_request("PUT", f"/repos/{GITHUB_REPO}/contents/{filepath}", {
        "message": commit_message,
        "content": encoded,
        "sha": sha,
        "branch": GITHUB_BRANCH,
    })
    commit_url = result["commit"]["html_url"]
    print(f"[INFO] Committed: {commit_url}")
    return commit_url


def list_python_files(directory: str = "") -> list[str]:
    """레포의 Python 파일 목록 반환"""
    path = f"/repos/{GITHUB_REPO}/contents/{directory}?ref={GITHUB_BRANCH}"
    items = github_request("GET", path)
    return [
        item["path"] for item in items
        if item["type"] == "file" and item["name"].endswith(".py")
    ]


def call_claude(user_request: str, current_code: str, filepath: str) -> str:
    """Claude API 호출. 수정된 코드만 반환"""
    system_prompt = """너는 Python 코드를 수정하는 전문 에이전트야.
규칙:
1. 수정된 전체 Python 코드만 반환해. 설명, 마크다운 코드블록(```), 주석 없이.
2. 요청한 부분만 최소한으로 수정해. 나머지 코드는 그대로 유지해.
3. 코드 품질(타입힌트, 에러핸들링)은 유지하거나 개선해."""

    user_prompt = f"""파일: {filepath}

요청: {user_request}

현재 코드:
{current_code}

수정된 전체 코드를 반환해줘."""

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API error {e.code}: {e.read().decode()}")


def parse_target_file(text: str) -> str | None:
    """메시지에서 파일명 추출"""
    match = re.search(r"([\w/]+\.py)", text)
    return match.group(1) if match else None


def lambda_handler(event: dict, context) -> None:
    """
    Worker Lambda 진입점.
    Receiver Lambda로부터 비동기 호출됨.
    """
    channel = event.get("channel", "")
    raw_text = event.get("text", "")

    # 멘션 제거
    user_request = re.sub(r"<@[A-Z0-9]+>\s*", "", raw_text).strip()
    print(f"[INFO] Request: {user_request}")

    # ── 테스트: 수신 확인 메시지만 발송 ──
    slack_webhook_post("🔧 워커에서 메세지를 수신했습니다.")

    # ── 운영 전환 시 아래 주석 해제 ──
    # try:
    #     target_file = parse_target_file(user_request)
    #
    #     if not target_file:
    #         py_files = list_python_files()
    #         file_list = "\n".join(f"  • {f}" for f in py_files)
    #         slack_webhook_post(f"어떤 파일을 수정할까요?\n{file_list}\n\n예) `handler.py timeout 30초로 늘려줘`")
    #         return
    #
    #     slack_webhook_post(f"`{target_file}` 읽는 중...")
    #     current_code, sha = get_file(target_file)
    #
    #     slack_webhook_post("Claude가 수정 중...")
    #     new_code = call_claude(user_request, current_code, target_file)
    #
    #     commit_msg = f"fix: {user_request[:60]}"
    #     commit_url = commit_file(target_file, new_code, sha, commit_msg)
    #
    #     slack_webhook_post(
    #         f"✅ 수정 완료!\n"
    #         f"파일: `{target_file}`\n"
    #         f"커밋: {commit_url}\n"
    #         f"GitHub Actions가 Lambda 배포를 시작해요."
    #     )
    #
    # except Exception as e:
    #     print(f"[ERROR] {e}")
    #     slack_webhook_post(f"❌ 오류가 발생했어요.\n```{str(e)}```")
