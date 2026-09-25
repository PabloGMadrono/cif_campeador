"""Pytest options shared by the OCR accuracy benchmarks."""

import os

import pytest

from tests.invoice_accuracy_v2 import OcrScope


def pytest_addoption(parser):
    parser.addoption(
        "--ocr-scope",
        action="store",
        default=os.environ.get("OCR_TEST_SCOPE", OcrScope.ALL.value),
        choices=[scope.value for scope in OcrScope],
        help="Select v2 OCR documents: valid, invalid, valid-invalid, review, or all.",
    )
    parser.addoption(
        "--ocr-image",
        action="store",
        default=None,
        help="Run the v2 OCR benchmark for one ground-truth image (filename or stem).",
    )


@pytest.fixture(scope="session")
def ocr_scope(pytestconfig) -> OcrScope:
    return OcrScope(pytestconfig.getoption("--ocr-scope"))


@pytest.fixture(scope="session")
def ocr_image(pytestconfig) -> str | None:
    return pytestconfig.getoption("--ocr-image")
