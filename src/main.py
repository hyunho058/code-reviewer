import io
import json
import logging
import os
import re
from typing import Optional

import openai
import requests
from github import Github
from unidiff import PatchSet

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_MODEL = os.getenv("OPENAI_API_MODEL")

client = openai.OpenAI(api_key=OPENAI_API_KEY)
github_client = Github(GITHUB_TOKEN)

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


class PullRequestDetails:
    def __init__(self, owner: str, repo: str, pull_number: int, title: str, description: str):
        self.owner = owner
        self.repo = repo
        self.pull_number = pull_number
        self.title = title
        self.description = description


def get_pull_request_details() -> PullRequestDetails:
    logger.debug("Getting PR details...")
    with open(os.getenv("GITHUB_EVENT_PATH"), "r") as file:
        event_data = json.load(file)

    owner = event_data["repository"]["owner"]["login"]
    repo = event_data["repository"]["name"]
    pull_number = event_data["pull_request"]["number"]

    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    pr = repo_obj.get_pull(pull_number)

    logger.debug(f"PR details: owner={owner}, repo={repo_obj.name}, pull_number={pull_number}, title={pr.title}")

    return PullRequestDetails(
        owner=owner,
        repo=repo_obj.name,
        pull_number=pull_number,
        title=pr.title,
        description=pr.body
    )


def get_diff(owner: str, repo: str, pull_number: int) -> Optional[str]:
    logger.debug(f"Getting diff for PR {pull_number} in repo {owner}/{repo}...")
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}"
    response = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"})
    pr_data = response.json()
    diff_url = pr_data.get("diff_url")

    if diff_url:
        diff_response = requests.get(diff_url)
        logger.debug("Diff fetched successfully.")
        return diff_response.text

    logger.warning(f"No diff URL found for PR {pull_number}")
    return None


def create_prompt(aggregated_diff: str, pr_details: PullRequestDetails) -> str:
    return f"""
You are an automated code review assistant. Your review output **must** follow the structure below **exactly**:

[AI Review]

**1.개요**
(이 Pull Request의 요약 및 주요 변경 사항을 간단히 설명)

**2.분석 영역**

2.1 런타임 오류 검사
(런타임 에러 가능성, NPE, IndexError 등)

2.2 성능 최적화
(비효율적인 루프, 불필요한 연산, 리소스 낭비, DB 호출 최적화 등)

2.3 코드 스타일 및 가독성
(가독성, 네이밍, 불필요한 코드, 포맷팅, 클래스/메서드 분리 등)

2.4 취약점 분석
- 접근 통제 취약점
- 암호화 실패
- 인젝션
- 안전하지 않은 설계
- 보안 설정 오류
- 취약하고 오래된 구성요소
- 식별 및 인증 실패
- 소프트웨어 및 데이터 무결성 실패
- 보안 로깅 및 모니터링 실패
- 서버 사이드 요청 위조(SSRF)
- 사용되지 않거나 안전하지 않은 모듈 사용
- 검증되지 않은 입력 처리
- 민감한 데이터의 부적절한 처리
- 민감한 정보 노출 (예: 하드코딩된 비밀번호)
- 기타 보안 위험

(위 항목들 중 발견된 취약점 또는 개선 사항이 있으면 제시하고, 없다면 '결과: 취약점 없음' 식으로 표기)

**3.종합 의견**
(최종 요약 및 의견 제시)

##중요##:
- 절대로 코드블록(\`\`\`)이나 JSON 포맷이 아닌 **위의 텍스트 구조** 그대로만 출력하세요.
- **긍정적 코멘트나 칭찬은 작성하지 말고**, 개선점이 있는 경우에만 작성하세요.
- 만약 개선할 점이 전혀 없다면, 2번 항목(분석 영역)에서 각 섹션에 "발견되지 않음"이라고 쓰고, 3번 항목에서도 별도 개선점 없이 마무리하세요.
- **2.분석 영역 항목에 대한 의견을 작성할때는 다음과 같이 코드 블록을 작성하세요** **(예시):

수정 전:
```java
기존 java 코드블럭
```

수정 후:
```java
개선된 java 코드블럭
```
**

Pull request title: {pr_details.title}
Pull request description:
---
{pr_details.description}
---

아래는 Pull Request에서 변경된 코드 diff 전체입니다:
(diff 시작)
{aggregated_diff}
(diff 끝)

분석 결과를 위의 구조대로 작성해주세요.
"""


def get_ai_review_text(prompt: str) -> str:
    logger.debug("Requesting AI review response...")
    try:
        response = client.chat.completions.create(
            model=OPENAI_API_MODEL,
            messages=[{"role": "system", "content": prompt}],
            max_tokens=1000,
            temperature=0.2,
        )
        logger.debug(f"AI response: {response}")

        content = response.choices[0].message.content

        content = re.sub(r"```(\w+)?", "", content)
        content = content.replace("```", "").strip()

        return content

    except Exception as error:
        logger.error(f"Error getting AI response: {error}")
    return ""


# github api document [https://docs.github.com/ko/rest/pulls/reviews]
def create_issue_comment(owner: str, repo: str, pull_number: int, body: str):
    logger.debug(f"Creating single issue comment for PR {pull_number}...")
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pull_number}/comments"

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {"body": body}

    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 201:
        logger.info("Issue comment created successfully.")
    else:
        logger.error(f"Failed to create issue comment: {response.text}")


def analyze_code(parsed_diff: PatchSet, pr_details: PullRequestDetails) -> str:
    logger.debug("Analyzing code diff...")

    aggregated_diff_lines = []
    for file in parsed_diff:
        if file.path == "/dev/null":
            continue

        aggregated_diff_lines.append(f"diff --git a/{file.path} b/{file.path}")
        for hunk in file:
            for line in hunk:
                if line.is_added:
                    aggregated_diff_lines.append(f"+ {line.value.strip()}")

    if not aggregated_diff_lines:
        logger.debug("No added lines found in this PR.")
        return ""

    aggregated_diff = "\n".join(aggregated_diff_lines)

    prompt = create_prompt(aggregated_diff, pr_details)
    ai_review_text = get_ai_review_text(prompt)
    return ai_review_text


def main():
    logger.info("Starting PR review process...")
    pr_details = get_pull_request_details()

    with open(os.getenv("GITHUB_EVENT_PATH"), "r") as file:
        event_data = json.load(file)
    logger.debug(f"Event data: {event_data}")

    if event_data["action"] in ["opened", "synchronize"]:
        diff = get_diff(pr_details.owner, pr_details.repo, pr_details.pull_number)
    else:
        logger.warning(f"Unsupported event: {event_data['action']}")
        return

    if not diff:
        logger.warning("No diff found.")
        return

    parsed_diff = PatchSet(io.StringIO(diff))

    review_text = analyze_code(parsed_diff, pr_details)
    if not review_text:
        logger.info("No comments generated by AI.")
        return

    create_issue_comment(pr_details.owner, pr_details.repo, pr_details.pull_number, review_text)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.error(f"Error: {error}")
