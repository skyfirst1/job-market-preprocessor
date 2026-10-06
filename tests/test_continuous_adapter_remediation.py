import json
from pathlib import Path

from scripts.remediate_continuous_adapters import metrics, normalize_zhiye_detail, quality


def test_zhiye_offline_detail_reparse_creates_one_complete_job(tmp_path):
    artifact = tmp_path / "detail.html"
    artifact.write_text(
        "<html><head><title>视觉算法工程师</title></head><body><main>"
        "<h1>视觉算法工程师</h1><p>负责医学图像深度学习模型开发和部署，"
        "包括数据治理、训练评估、误差分析、性能优化和生产监控。</p>"
        "<p>要求熟悉 Python、PyTorch、计算机视觉与图像分割，"
        "能够独立完成实验设计、模型验证、技术文档和跨团队交付。</p>"
        "</main></body></html>", encoding="utf-8")
    before = {"status": "partial", "html_path": str(artifact), "jobs": [],
              "text": "", "coverage": {"complete": False, "list_complete": False}}

    after = normalize_zhiye_detail(before, "https://example.zhiye.com/campus/detail?jobAdId=1")

    assert after["status"] == "ok"
    assert len(after["jobs"]) == 1
    assert after["jobs"][0]["needs_details"] is False
    assert after["coverage"]["complete"] is True
    assert after["offline_reparse"]["network_accessed"] is False


def test_quality_prefers_complete_or_more_jobs():
    partial = metrics("partial", {"jobs": [], "text": "x", "coverage": {}})
    complete = metrics("ok", {"jobs": [{"title": "A"}], "text": "x",
                               "coverage": {"complete": True, "list_complete": True}})
    assert quality(complete) > quality(partial)
