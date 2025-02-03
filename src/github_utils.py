from github import Github
import os
import json

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
github = Github(GITHUB_TOKEN)


def get_pr_details(event_path):
    """GitHub 이벤트 데이터에서 PR 정보를 가져옴"""
    with open(event_path, "r") as f:
        event_data = json.load(f)

    repo_name = event_data["repository"]["full_name"]
    pr_number = event_data["number"]

    repo = github.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    return {
        "owner": repo.owner.login,
        "repo": repo.name,
        "pull_number": pr_number,
        "title": pr.title,
        "description": pr.body,
    }


def get_diff(repo_full_name, pr_number):
    """PR의 diff 데이터를 가져옴"""
    repo = github.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    diff_text = ""
    for file in pr.get_files():
        if file.patch:
            diff_text += f"Filename: {file.filename}\n{file.patch}\n\n"

    print("=== DIFF TEXT ===")
    print(diff_text)
    print("================")

    return diff_text