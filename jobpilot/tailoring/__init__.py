"""
Tailored resume and cover-letter generation for high-scoring jobs.

    master resume + job description -> LLM (JSON) -> fact check -> .docx files
"""

from jobpilot.tailoring.generator import DocumentGenerator, GenerationResult

__all__ = ["DocumentGenerator", "GenerationResult"]
