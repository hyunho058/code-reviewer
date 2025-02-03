import os
import json
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
    # GitHub 이벤트에서 PR 정보 읽어오기
    with open(os.getenv("GITHUB_EVENT_PATH"), "r") as file:
        event_data = json.load(file)

    owner = event_data["repository"]["owner"]["login"]
    repo = event_data["repository"]["name"]
    pull_number = event_data["pull_request"]["number"]

    repo = github_client.get_repo(f"{owner}/{repo}")
    pr = repo.get_pull(pull_number)

    logger.debug(f"PR details: owner={owner}, repo={repo.name}, pull_number={pull_number}, title={pr.title}")

    return PullRequestDetails(
        owner=owner,
        repo=repo.name,
        pull_number=pull_number,
        title=pr.title,
        description=pr.body
    )


def get_diff(owner: str, repo: str, pull_number: int) -> Optional[str]:
    logger.debug(f"Getting diff for PR {pull_number} in repo {owner}/{repo}...")
    # PR의 diff 데이터를 GitHub API에서 가져오기
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


def create_prompt(file_path: str, chunk: dict, pr_details: PullRequestDetails) -> str:
    if not isinstance(chunk, dict) or "content" not in chunk:
        logger.error("Invalid chunk format")
        return ""

    return f"""
Your task is to review pull requests with a focus on **Object-Oriented Programming (OOP), code readability, and performance optimization**.
Instructions:
- Provide the response in following JSON format:  {{"reviews": [{{"lineNumber":  <line_number>, "reviewComment": "<review comment>"}}]}}
- **Do not give positive comments or compliments.**
- **Provide comments ONLY if there is something to improve.** If the code is fine, return an empty array: `"reviews": []`
- **Write comments in GitHub Markdown format.**
- **Focus on the following aspects when reviewing the code:**
  1. **Object-Oriented Design (OOP)**:
     - Does the code **follow SOLID principles** (Single Responsibility, Open-Closed, Liskov Substitution, Interface Segregation, Dependency Inversion)?
     - Is there **tight coupling** that should be reduced?
     - Should any logic be moved to a separate class or method for better reusability?
     - Are there unnecessary static methods that could be refactored into instance methods?
  2. **Code Readability**:
     - Are variable and method names **clear and descriptive**?
     - Is the **indentation and formatting consistent**?
     - Are there **redundant or unnecessary lines of code**?
  3. **Performance Optimization**:
     - Are there **unnecessary loops, inefficient algorithms, or redundant calculations**?
     - Are there **costly database calls or API requests inside loops**?
     - Does the code **handle large inputs efficiently**?
     - Should caching be considered to improve performance?

**Review the following code diff** in the file "{file_path}" and take the pull request title and description into account when writing the response.

Pull request title: {pr_details.title}
Pull request description:

---
{pr_details.description}
---

Git diff to review:

```diff
{chunk["content"]}
```"""



def get_ai_response(prompt: str, file_path: str) -> Optional[List[Dict[str, str]]]:
    logger.debug("Requesting AI review response...")
    try:
        response = client.chat.completions.create(  # 최신 API 방식 사용
            model=OPENAI_API_MODEL,
            messages=[{"role": "system", "content": prompt}],
            max_tokens=700,
            temperature=0.2,
        )

        logger.debug(f"AI response: {response}")
        reviews = json.loads(response.choices[0].message.content).get("reviews", [])

        for review in reviews:
            review["path"] = file_path

        return reviews
    except Exception as error:
        logger.error(f"Error getting AI response: {error}")
    return []


def create_comment(file_path: str, line, ai_responses: List[Dict[str, str]]) -> List[Dict[str, str]]:
    logger.debug(f"Creating comments for file {file_path}...")
    comments = []

    line_number = line.target_line_no  # 정확한 line number
    if line_number <= 0:
        logger.warning(f"Skipping invalid comment due to invalid line number: {line_number}")
        return []

    for ai_response in ai_responses:
        comments.append({
            "body": ai_response["reviewComment"],
            "path": file_path,
            "line": line_number
        })

    logger.debug(f"Generated {len(comments)} comments.")
    return comments


def create_review_comment(owner: str, repo: str, pull_number: int, comments: List[Dict[str, str]]):
    logger.debug(f"Creating review comments for PR {pull_number}...")

    valid_comments = []
    for comment in comments:
        if comment["line"] > 0:
            valid_comments.append({
                "body": comment["body"],
                "path": comment["path"],
                "line": comment["line"],
                "side": "RIGHT"
            })
        else:
            logger.warning(f"Skipping invalid comment: {comment}")

    if not valid_comments:
        logger.info("No valid comments to post.")
        return

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "event": "COMMENT",
        "comments": valid_comments
    }

    logger.debug(f"Review comments data: {data}")
    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 200:
        logger.info("Review comments created successfully.")
    else:
        logger.error(f"Failed to create review comment: {response.text}")


def analyze_code(parsed_diff: PatchSet, pr_details: PullRequestDetails) -> List[Dict[str, str]]:
    logger.debug("Analyzing code diff...")
    comments = []

    for file in parsed_diff:
        logger.debug(f"Processing file: {file.path}")

        if file.path == "/dev/null":
            continue

        for hunk in file:
            logger.debug(f"hunk: {hunk}")
            for line in hunk:
                if line.is_added:
                    logger.debug(f"Processing added line {line.target_line_no}: {line.value.strip()}")
                    prompt = create_prompt(file.path, {"content": line.value.strip()}, pr_details)
                    ai_response = get_ai_response(prompt, file.path)
                    if ai_response:
                        new_comments = create_comment(file.path, line, ai_response)
                        comments.extend(new_comments)

    logger.debug(f"Total {len(comments)} comments analyzed.")
    return comments


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
        logger.warning("No diff found")
        return

    parsed_diff = PatchSet(io.StringIO(diff))  # 문자열을 PatchSet 객체로 변환

    comments = analyze_code(parsed_diff, pr_details)

    if comments:
        create_review_comment(pr_details.owner, pr_details.repo, pr_details.pull_number, comments)
    else:
        logger.info("No comments generated.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.error(f"Error: {error}")
