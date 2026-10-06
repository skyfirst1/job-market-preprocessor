from scripts.audit_recovered_jobs_target_delta import classify


def test_plain_image_processing_requires_affirmative_ai_evidence():
    priority, _, evidence, exclusion_code, exclusion_evidence = classify(
        "负责医学影像处理、图像配准、重建、去噪与OpenCV工程实现"
    )

    assert priority is None
    assert evidence == ""
    assert exclusion_code == "image_processing_without_ai_evidence"
    assert "医学影像" in exclusion_evidence


def test_pretrained_image_api_integration_is_not_visual_ai_ownership():
    priority, _, _, exclusion_code, exclusion_evidence = classify(
        "接入预训练视觉模型并调用图像识别API完成业务集成"
    )

    assert priority is None
    assert exclusion_code == "image_processing_without_ai_evidence"
    assert "预训练视觉模型" in exclusion_evidence


def test_visual_model_training_is_category_one():
    priority, category, evidence, exclusion_code, _ = classify(
        "负责基于ViT的医学影像分割模型训练、评估和优化"
    )

    assert priority == 1
    assert category == "视觉相关AI/深度学习算法"
    assert "ViT" in evidence
    assert exclusion_code == ""


def test_ai_algorithm_collaboration_is_not_model_ownership():
    priority, _, _, exclusion_code, _ = classify(
        "与AI算法工程师紧密配合，为AI模型训练提供领域知识指导"
    )

    assert priority is None
    assert exclusion_code == ""
