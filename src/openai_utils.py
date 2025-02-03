import openai
import os
import json

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_MODEL = os.getenv("OPENAI_API_MODEL", "gpt-4")

client = openai.OpenAI(api_key=OPENAI_API_KEY)  # 새로운 클라이언트 객체 사용

def generate_review_comment(file_path, changes, pr_details):
    """OpenAI API를 호출하여 코드 리뷰 생성"""
    print("=== generate_review_comment ===")
    print(pr_details)
    print("================")
    prompt = f"""
    Your task is to review pull requests. Instructions:
    - Provide JSON response: {{"reviews": [{{"lineNumber": <line_number>, "reviewComment": "<review comment>"}}]}}
    - Provide comments ONLY for necessary improvements, else "reviews": []
    - Write the comment in GitHub Markdown format.

    Reviewing file: {file_path}

    Pull request title: {pr_details["title"]}
    Pull request description:

    ---
    {pr_details["description"]}
    ---

    Changes:
    {changes}
    """

    try:
        print("========generate_review_comment 1 prompt ========")
        print(prompt)
        response = client.chat.completions.create(  # 최신 API 방식 사용
            model=OPENAI_API_MODEL,
            messages=[{"role": "system", "content": prompt}],
            max_tokens=700,
            temperature=0.2,
        )

        print("======== response ========")
        print(response)

        reviews = json.loads(response.choices[0].message.content).get("reviews", [])

        for review in reviews:
            review["path"] = file_path

        return reviews

        # return json.loads(response.choices[0].message.content).get("reviews", [])

    except Exception as e:
        print(f"OpenAI API Error: {e}")
        return []
