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

# 수정 대상 파일 경로 (환경변수로 지정, 콤마로 복수 지정 가능)
# 예: "src/main.py,src/utils.py"
TARGET_FILES = os.environ.get("TARGET_FILES", "")

# AWS Secrets Manager secret 이름
GITHUB_SECRET_NAME = "pocket-dev-agent-github-token-fine-grained-pocket-dev-agent"
ANTHROPIC_SECRET_NAME = os.environ.get("ANTHROPIC_SECRET_NAME", "pocket-dev-agent-anthropic-api-key")

# Claude 모델
CLAUDE_MODEL = "claude-opus-4-5"

# Lambda 실행 중 토큰 캐싱 (cold start 시에만 Secrets Manager 호출)
_github_token_cache: str | None = None
_anthropic_key_cache: str | None = None


# ── Secrets Manager ──────────────────────────────────

def _get_secret(secret_name: str) -> str:
    """AWS Secrets Manager에서 시크릿 문자열을 가져옴."""
    client = boto3.client("secretsmanager", region_name="ap-northeast-2")
    response = client.get_secret_value(SecretId=secret_name)
    secret = response["SecretString"]
    try:
        parsed = json.loads(secret)
        return parsed.get("token") or parsed.get("api_key") or list(parsed.values())[0]
    except (json.JSONDecodeError, IndexError):
        return secret.strip()


def get_github_token() -> str:
    global _github_token_cache
    if _github_token_cache:
        return _github_token_cache
    print(f"[INFO] Fetching GitHub secret: {GITHUB_SECRET_NAME}")
    _github_token_cache = _get_secret(GITHUB_SECRET_NAME)
    return _github_token_cache


def get_anthropic_key() -> str:
    global _anthropic_key_cache
    if _anthropic_key_cache:
        return _anthropic_key_cache
    print(f"[INFO] Fetching Anthropic secret: {ANTHROPIC_SECRET_NAME}")
    _anthropic_key_cache = _get_secret(ANTHROPIC_SECRET_NAME)
    return _anthropic_key_cache


# ── Slack ─────────────────────────────────────────────

def slack_webhook_post(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        print("[WARN] SLACK_WEBHOOK_URL not set")
        return
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        print(f"[INFO] Slack webhook response: {resp.status}")


# ── GitHub ────────────────────────────────────────────

def github_request(method: str, path: str, data: dict = None) -> dict:
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
        data = github_request(
            "GET",
            f"/repos/{GITHUB_REPO}/contents/{filepath}?ref={GITHUB_BRANCH}",
        )
        content = base64.b64decode(data["content"]).decode("utf-8")
        sha = data["sha"]
        print(f"[INFO] Read file: {filepath} (sha={sha[:7]})")
        return content, sha
    except RuntimeError as e:
        if "404" in str(e):
            print(f"[INFO] File not found: {filepath}")
            return None, None
        raise


def get_repo_tree() -> list[str]:
    """
    GitHub Git Trees API로 레포 전체 파일 목록(blob만) 반환.
    truncated 경우 경고만 출력하고 가능한 목록 반환.
    """
    data = github_request(
        "GET",
        f"/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1",
    )
    if data.get("truncated"):
        print("[WARN] Repo tree truncated — large repo. Returning partial list.")
    files = [item["path"] for item in data.get("tree", []) if item["type"] == "blob"]
    print(f"[INFO] Repo tree: {len(files)} files found")
    return files


def commit_file(filepath: str, new_content: str, sha: str | None, commit_message: str) -> str:
    """GitHub에 파일 커밋. 커밋 URL 반환"""
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


# ── Claude API ────────────────────────────────────────

def build_system_prompt() -> str:
    return """당신은 코드 수정 에이전트입니다.
사용자 요청, 레포지토리 파일 구조, 파일 내용을 분석하고 수정이 필요한 파일의 전체 코드를 반환합니다.

레포지토리 파일 구조를 참고하여 요청과 관련된 파일을 스스로 판단하세요.
파일 내용이 제공되지 않은 파일도 파일 경로만 보고 수정 대상을 결정할 수 있습니다.

반드시 아래 JSON 형식만 응답하세요. 설명, 마크다운 코드블록, 기타 텍스트 없이 JSON만 출력하세요.

{
  "summary": "변경 사항 요약 (한국어, 1~2문장)",
  "commit_message": "git commit 메시지 (영어, 50자 이내)",
  "files": [
    {
      "filepath": "수정할 파일 경로 (레포 루트 기준)",
      "content": "파일 전체 내용 (수정 반영)",
      "reason": "이 파일을 수정한 이유"
    }
  ]
}

수정이 필요 없는 파일은 files 배열에 포함하지 마세요.
수정이 전혀 필요 없으면 files를 빈 배열로 반환하세요."""


def build_user_prompt(
    user_message: str,
    repo_tree: list[str],
    file_contexts: list[dict],
) -> str:
    parts = [f"## 요청\n{user_message}\n"]

    if repo_tree:
        tree_text = "\n".join(repo_tree)
        parts.append(f"## 레포지토리 파일 구조\n```\n{tree_text}\n```\n")

    if file_contexts:
        parts.append("## 현재 파일 내용")
        for fc in file_contexts:
            if fc["content"] is not None:
                parts.append(f"\n### {fc['filepath']}\n```\n{fc['content']}\n```")
            else:
                parts.append(f"\n### {fc['filepath']}\n(파일 없음 — 새로 생성 필요 시 포함)")

    return "\n".join(parts)


def call_claude(
    user_message: str,
    repo_tree: list[str],
    file_contexts: list[dict],
) -> dict:
    """Claude API 호출 후 JSON 응답 파싱"""
    api_key = get_anthropic_key()
    url = "https://api.anthropic.com/v1/messages"

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 8192,
        "system": build_system_prompt(),
        "messages": [
            {
                "role": "user",
                "content": build_user_prompt(user_message, repo_tree, file_contexts),
            }
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Anthropic API error {e.code}: {e.read().decode()}")

    raw_text = result["content"][0]["text"].strip()

    # 혹시 마크다운 코드블록이 포함된 경우 제거
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude 응답 JSON 파싱 실패: {e}\n원문:\n{raw_text}")


# ── Lambda 진입점 ─────────────────────────────────────

def extract_message(event: dict) -> str:
    """
    다양한 트리거 형태에서 메시지 문자열 추출.
    - SQS: Records[0].body (문자열 또는 JSON)
    - SNS: Records[0].Sns.Message
    - 직접 invoke: event.message 또는 event 자체를 문자열화
    """
    # SQS
    records = event.get("Records", [])
    if records:
        record = records[0]
        if "body" in record:
            body = record["body"]
            try:
                parsed = json.loads(body)
                # SNS → SQS 경유 패턴
                if "Message" in parsed:
                    return parsed["Message"]
                return parsed.get("message", body)
            except (json.JSONDecodeError, TypeError):
                return body
        # SNS 직접
        if "Sns" in record:
            return record["Sns"]["Message"]

    # 직접 invoke
    if "message" in event:
        return event["message"]

    return json.dumps(event, ensure_ascii=False)


def lambda_handler(event: dict, context) -> dict:
    """
    Worker Lambda 진입점.
    1. 이벤트에서 메시지 추출
    2. 대상 파일을 GitHub에서 읽기
    3. Claude에게 메시지 + 파일 내용 전달
    4. Claude가 반환한 수정 파일을 GitHub에 커밋
    5. Slack 알림
    """
    print(f"[INFO] Event received: {json.dumps(event)}")

    try:
        # 1. 메시지 추출
        user_message = extract_message(event)
        print(f"[INFO] User message: {user_message}")

        # 2. 레포 파일 트리 조회
        print("[INFO] Fetching repo tree from GitHub...")
        repo_tree = get_repo_tree()

        # 3. 대상 파일 목록 결정 (이벤트 > 환경변수 > 없으면 Claude가 판단)
        raw_targets = (
            event.get("target_files")
            or event.get("Records", [{}])[0].get("target_files", "")
            or TARGET_FILES
        )
        target_list = [f.strip() for f in raw_targets.split(",") if f.strip()]

        file_contexts = []
        sha_map: dict[str, str | None] = {}

        for filepath in target_list:
            content, sha = get_file(filepath)
            file_contexts.append({"filepath": filepath, "content": content})
            sha_map[filepath] = sha

        # 4. Claude 호출 (메시지 + 레포 트리 + 파일 내용)
        print("[INFO] Calling Claude API...")
        claude_response = call_claude(user_message, repo_tree, file_contexts)
        print(f"[INFO] Claude summary: {claude_response.get('summary', '')}")

        modified_files = claude_response.get("files", [])
        commit_message = claude_response.get("commit_message", "chore: update by dev-agent")
        summary = claude_response.get("summary", "변경 없음")

        if not modified_files:
            slack_webhook_post(
                f"🤖 Dev Agent 응답\n"
                f"요청: {user_message}\n"
                f"결과: 수정 사항 없음\n"
                f"요약: {summary}"
            )
            return {"status": "no_changes", "summary": summary}

        # 5. 수정된 파일 커밋
        #    sha_map에 없는 파일(Claude가 새로 제안한 파일)은 GitHub에서 sha 확인
        commit_urls = []
        for file_info in modified_files:
            filepath = file_info["filepath"]
            new_content = file_info["content"]

            if filepath not in sha_map:
                # Claude가 target_files 외 파일을 수정 제안한 경우 sha 확인
                _, sha = get_file(filepath)
                sha_map[filepath] = sha

            sha = sha_map[filepath]
            url = commit_file(filepath, new_content, sha, commit_message)
            commit_urls.append(f"• `{filepath}` → {url}")
            print(f"[INFO] Committed {filepath}")

        # 6. Slack 알림
        commits_text = "\n".join(commit_urls)
        slack_webhook_post(
            f"✅ Dev Agent 코드 수정 완료\n"
            f"요청: {user_message}\n"
            f"요약: {summary}\n"
            f"커밋:\n{commits_text}"
        )

        return {
            "status": "success",
            "summary": summary,
            "commits": commit_urls,
        }

    except Exception as e:
        print(f"[ERROR] {e}")
        slack_webhook_post(f"❌ Dev Agent 오류\n```{str(e)}```")
        raise
