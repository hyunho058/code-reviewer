import os
import json
import re
import openai
import requests
import logging
from github import Github
from typing import List, Dict, Optional
from unidiff import PatchSet
import io

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
    """
    PR 전체 변경사항(aggregated_diff)을 하나로 묶어 AI에게 전달할 프롬프트를 생성.
    """
    return f"""
Your task is to review this entire pull request with a focus on **Object-Oriented Programming (OOP), code readability, and performance optimization**.
Instructions:
- Provide the response in following JSON format:  {{"reviews": [{{"lineNumber": <line_number>, "reviewComment": "<review comment>"}}]}}
- **Do not give positive comments or compliments.**
- **Provide comments ONLY if there is something to improve.** If the code is fine, return an empty array: `"reviews": []`
- **Write comments in GitHub Markdown format.**
- **Focus on the following aspects when reviewing the code:**
  1. **Object-Oriented Design (OOP)**:
     - Does the code **follow SOLID principles**?
     - Is there **tight coupling** that should be reduced?
     - Could logic be moved to a separate class or method for better reusability?
  2. **Code Readability**:
     - Are variable and method names **clear and descriptive**?
     - Is the **indentation and formatting consistent**?
     - Are there **redundant or unnecessary lines of code**?
  3. **Performance Optimization**:
     - Any **unnecessary loops, inefficient algorithms, or redundant calculations**?
     - Any **costly database calls or API requests inside loops**?
     - Could the code **handle large inputs more efficiently**?

Pull request title: {pr_details.title}
Pull request description:
---
{pr_details.description}
---

Below is the aggregated diff of all files changed in this PR:

```diff
{aggregated_diff}
```"""


def get_ai_response(prompt: str) -> Optional[List[Dict[str, str]]]:
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

        data = json.loads(content)
        reviews = data.get("reviews", [])
        return reviews

    except Exception as error:
        logger.error(f"Error getting AI response: {error}")
    return []


def combine_reviews_into_single_comment(reviews: List[Dict[str, str]]) -> str:
    """
    AI가 반환한 여러 리뷰를 하나의 문자열로 합침.
    (lineNumber는 여기서는 참고용으로만 사용하거나, 필요없으면 생략 가능)
    """
    if not reviews:
        return ""

    comment_lines = []
    for review in reviews:
        ln = review.get("lineNumber", "N/A")
        comment_body = review.get("reviewComment", "")
        comment_lines.append(f"**Line {ln}**:\n{comment_body}\n")

    # 여러 리뷰 사이에 줄바꿈 추가
    return "\n".join(comment_lines).strip()


def create_issue_comment(owner: str, repo: str, pull_number: int, body: str):
    """
    PR(이슈) 하단에 단일 코멘트(이슈 코멘트)를 작성하는 함수.
    """
    logger.debug(f"Creating single issue comment for PR {pull_number}...")
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pull_number}/comments"

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "body": body
    }

    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 201:
        logger.info("Issue comment created successfully.")
    else:
        logger.error(f"Failed to create issue comment: {response.text}")


def analyze_code(parsed_diff: PatchSet, pr_details: PullRequestDetails) -> str:
    """
    1) 모든 파일의 '추가된 라인'을 합쳐서 하나의 aggregated_diff를 만든 뒤,
    2) 그걸 AI에 전달하여 종합 리뷰를 얻고,
    3) 리뷰 내용을 단일 문자열로 반환.
    """
    logger.debug("Analyzing code diff...")

    # 1) 모든 파일에서 추가된 라인만 모아서 하나의 diff 문자열 생성
    aggregated_diff_lines = []

    for file in parsed_diff:
        if file.path == "/dev/null":
            continue

        # 파일 헤더를 diff 스타일로 추가 (선택사항)
        aggregated_diff_lines.append(f"diff --git a/{file.path} b/{file.path}")
        for hunk in file:
            for line in hunk:
                if line.is_added:
                    # 실제 diff 표기: 앞에 '+' 붙이기
                    aggregated_diff_lines.append(f"+ {line.value.strip()}")

    if not aggregated_diff_lines:
        logger.debug("No added lines found in this PR.")
        return ""  # 변경사항이 없으면 빈 문자열

    aggregated_diff = "\n".join(aggregated_diff_lines)

    # 2) AI에 리뷰 요청
    prompt = create_prompt(aggregated_diff, pr_details)
    ai_reviews = get_ai_response(prompt)
    if not ai_reviews:
        logger.debug("No AI reviews returned or empty array.")
        return ""  # AI가 별다른 리뷰가 없으면 빈 문자열

    # 3) 리뷰 결과를 하나의 문자열로 합침
    comment_body = combine_reviews_into_single_comment(ai_reviews)
    return comment_body


def main():
    logger.info("Starting PR review process...")
    pr_details = get_pull_request_details()

    with open(os.getenv("GITHUB_EVENT_PATH"), "r") as file:
        event_data = json.load(file)
    logger.debug(f"Event data: {event_data}")

    # PR action이 열리거나 동기화(synchronize)된 경우에만 동작
    if event_data["action"] in ["opened", "synchronize"]:
        diff = get_diff(pr_details.owner, pr_details.repo, pr_details.pull_number)
    else:
        logger.warning(f"Unsupported event: {event_data['action']}")
        return

    if not diff:
        logger.warning("No diff found.")
        return

    parsed_diff = PatchSet(io.StringIO(diff))

    # 종합 리뷰 생성
    comment_body = analyze_code(parsed_diff, pr_details)
    if not comment_body:
        logger.info("No comments generated by AI.")
        return

    # 생성된 리뷰를 PR(이슈) 하단에 단일 코멘트로 작성
    create_issue_comment(pr_details.owner, pr_details.repo, pr_details.pull_number, comment_body)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.error(f"Error: {error}")
