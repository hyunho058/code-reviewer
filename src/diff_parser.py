from unidiff import PatchSet
import re

def parse_diff(diff_text):
    """Diff 데이터를 정리한 후 파싱"""
    if not diff_text.strip():
        print("Warning: Empty diff content received.")
        return []

    try:
        # diff_text를 파일별로 분리
        file_diffs = diff_text.split("Filename: ")
        file_diffs = ["Filename: " + part for part in file_diffs if part.strip()]

        parsed_files = []

        for file_diff in file_diffs:
            patch_set = PatchSet(file_diff)

            for file in patch_set:
                if file.is_removed_file:
                    continue  # 삭제된 파일은 무시

                # 파일명 추출
                file_path_match = re.search(r'Filename: (.+)', file_diff)
                file_path = file_path_match.group(1).strip() if file_path_match else file.path

                print(f"Extracted file path: {file_path}")

                changes = []
                for hunk in file:
                    for line in hunk:
                        if line.is_added:
                            changes.append({"line": line.target_line_no, "content": line.value.strip()})

                parsed_files.append({"path": file_path, "changes": changes})

        return parsed_files

    except Exception as e:
        print(f"Diff Parsing Error: {e}")
        return []
