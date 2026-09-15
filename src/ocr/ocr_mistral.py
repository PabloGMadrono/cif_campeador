"""Mistral Document AI OCR and structured invoice annotations."""

import argparse
import base64
from contextlib import ExitStack
from functools import cached_property
from io import BytesIO
from pathlib import Path

from PIL import Image
from pydantic import TypeAdapter, ValidationError

from src.config import MISTRAL_API_KEY

from .models import Invoice
from .ocr_abc import Ocr_operator
from .ocr_openai import IMAGE_INVOICE_INSTRUCTIONS
from .preprocessing import prepare_document, image_data_url, lossless_pdf


_INVOICE_ADAPTER = TypeAdapter(Invoice)


class Ocr_mistral(Ocr_operator):
    """Extract Markdown or an Invoice directly using only Mistral credentials."""

    @cached_property
    def _ocr_client(self):
        if not MISTRAL_API_KEY or not MISTRAL_API_KEY.strip():
            raise RuntimeError("Set MISTRAL_API_KEY in your environment or .env")
        from mistralai.client import Mistral

        return Mistral(api_key=MISTRAL_API_KEY, timeout_ms=120_000)

    def extract_invoice(self, path: str) -> Invoice:
        """Annotate the entire invoice in one OCR call, then validate locally."""
        documents = self._documents(path)
        document = documents[0] if len(documents) == 1 else _images_as_pdf(documents)
        response = self._ocr_client.ocr.process(
            model="mistral-ocr-latest",
            document=document,
            include_image_base64=True,
            document_annotation_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "Invoice",
                    "schema_definition": _INVOICE_ADAPTER.json_schema(),
                    "strict": True,
                },
            },
            document_annotation_prompt=IMAGE_INVOICE_INSTRUCTIONS,
        )
        annotation = response.document_annotation
        if not isinstance(annotation, str) or not annotation.strip():
            raise RuntimeError("Mistral OCR returned no invoice annotation")
        try:
            return _INVOICE_ADAPTER.validate_json(annotation, strict=True)
        except ValidationError as error:
            raise RuntimeError("Mistral invoice annotation does not match the Invoice schema") from error

    @staticmethod
    def _documents(path: str) -> list[dict[str, str]]:
        """Validate input and encode PDF bytes or normalized image frames."""
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Document not found: {file_path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"Expected a document file: {file_path}")

        prepared = prepare_document(path)
        if file_path.suffix.lower() == ".pdf" and prepared.config["mode"] == "off":
            encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
            documents = [{
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{encoded}",
            }]
        else:
            with prepared.images() as images:
                if len(images) > 1:
                    encoded = base64.b64encode(lossless_pdf(images, prepared.config["pdf_dpi"])).decode("ascii")
                    documents = [{"type": "document_url", "document_url": f"data:application/pdf;base64,{encoded}"}]
                else:
                    documents = [{"type": "image_url", "image_url": image_data_url(images[0])}]
        if not documents:
            raise ValueError("Document contains no pages")
        return documents

    def extract_text(self, path: str) -> str:
        """Return Markdown in page order, without requesting invoice annotations."""
        texts = []
        for document in self._documents(path):
            response = self._ocr_client.ocr.process(
                model="mistral-ocr-latest",
                document=document,
                include_image_base64=True,
            )
            pages = getattr(response, "pages", None)
            if not pages:
                raise RuntimeError("Mistral OCR returned no pages")
            for page in pages:
                markdown = getattr(page, "markdown", None)
                if not isinstance(markdown, str):
                    raise RuntimeError("Mistral OCR returned a page without Markdown text")
                texts.append(markdown)
        return "\n\n".join(texts)


def _images_as_pdf(documents: list[dict[str, str]]) -> dict[str, str]:
    """Keep all image frames together for document-wide invoice annotation."""
    with ExitStack() as stack:
        pages = []
        for document in documents:
            data = base64.b64decode(document["image_url"].split(",", 1)[1])
            source = stack.enter_context(Image.open(BytesIO(data)))
            pages.append(stack.enter_context(source.convert("RGB")))
        encoded = base64.b64encode(lossless_pdf(pages)).decode("ascii")
    return {"type": "document_url", "document_url": f"data:application/pdf;base64,{encoded}"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract and print Markdown from an image or PDF using Mistral OCR."
    )
    parser.add_argument("path", help="Path to an image or PDF")
    args = parser.parse_args()
    print(Ocr_mistral().extract_text(args.path))


if __name__ == "__main__":
    main()
