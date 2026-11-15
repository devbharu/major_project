"""Pydantic contracts for everything the LLM returns."""
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "major", "minor", "nitpick"]


class Finding(BaseModel):
    severity: Severity = "minor"
    category: str = "Correctness"
    title: str
    body: str = ""
    impact: str = ""
    confidence: float = 0.8
    path: str | None = None
    line: int | None = None
    end_line: int | None = None
    suggestion: str | None = Field(default=None, description="Replacement code for lines line..end_line")


class ReviewOutput(BaseModel):
    summary: str = ""
    file_summaries: dict[str, str] = {}
    findings: list[Finding] = []


class VerifyItem(BaseModel):
    id: int
    keep: bool = True
    reason: str = ""


class VerifyOutput(BaseModel):
    items: list[VerifyItem] = []


class FixPatch(BaseModel):
    start_line: int
    end_line: int
    replacement: str
    explanation: str = ""
    test_path: str | None = None
    test_content: str | None = None


class FixCheck(BaseModel):
    resolved: bool
    reason: str = ""


class TriageNote(BaseModel):
    reason: str


class Layer(BaseModel):
    title: str
    description: str = ""
    files: list[str] = []


class BlastItem(BaseModel):
    area: str
    impact: str = ""
    severity: Literal["high", "medium", "low"] = "low"
    files: list[str] = []


class ArchItem(BaseModel):
    component: str
    change: str = ""
    note: str = ""


class StackOutput(BaseModel):
    summary: str = ""
    intent: str = ""
    risk: Literal["high", "medium", "low"] = "low"
    layers: list[Layer] = []
    blast_radius: list[BlastItem] = []
    architecture: list[ArchItem] = []


class ChatOutput(BaseModel):
    reply: str
    learning: str | None = None
    learning_path: str | None = None


class SecurityFinding(BaseModel):
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    title: str
    body: str = ""
    path: str
    line: int | None = None
    suggestion: str | None = None


class SecurityOutput(BaseModel):
    findings: list[SecurityFinding] = []


class CiEdit(BaseModel):
    path: str
    old: str = Field(description="Exact text to replace; must occur exactly once in the file")
    new: str


class CiNewFile(BaseModel):
    path: str
    content: str


class CiFix(BaseModel):
    root_cause: str
    solution: str
    confidence: float = 0.5
    edits: list[CiEdit] = []
    new_files: list[CiNewFile] = []
    verify_commands: list[str] = []
    needs_human: bool = False
    human_steps: str = ""


class Refined(BaseModel):
    text: str
