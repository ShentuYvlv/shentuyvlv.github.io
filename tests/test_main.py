import io
import unittest

import main


class ResumeAnalyzerTests(unittest.TestCase):
    def setUp(self):
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()

    def test_clean_resume_text_keeps_paragraphs(self):
        self.assertEqual(main.clean_resume_text("  张三\u00a0\t\n\nPython\r工程师 "), "张三\nPython\n工程师")

    def test_normalize_analysis_result_returns_stable_contract(self):
        result = main.normalize_analysis_result(
            {
                "basic_info": {"name": "张三"},
                "other_info": {"project_experience": ["招聘系统"]},
                "matching_analysis": {"score": 128, "skills_match_rate": "80"},
            }
        )
        self.assertEqual(result["basic_info"]["name"], "张三")
        self.assertEqual(result["matching_analysis"]["score"], 100)
        self.assertEqual(result["matching_analysis"]["skills_match_rate"], 80)
        self.assertEqual(result["other_info"]["project_experience"], ["招聘系统"])

    def test_analyze_requires_pdf_file_and_job_description(self):
        response = self.client.post("/analyze", data={"jd": "这是一个足够长的岗位描述，用于验证接口参数校验。"})
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            "/analyze",
            data={
                "resume": (io.BytesIO(b"not a pdf"), "resume.txt"),
                "jd": "这是一个足够长的岗位描述，用于验证接口参数校验。",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
