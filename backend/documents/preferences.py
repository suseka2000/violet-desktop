"""Editable, validated album text settings. Shared by UI and agent drafts."""
from typing import Annotated
from pydantic import Field
from ..domain.models import Model


class TitleReplacement(Model):
    from_title: Annotated[str, Field(min_length=1, max_length=300)]
    to_title: Annotated[str, Field(min_length=1, max_length=300)]


class AlbumNote(Model):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(min_length=1, max_length=12000)]


class DocumentPreferences(Model):
    title_replacements: list[TitleReplacement] = Field(default_factory=list, max_length=200)
    notes: list[AlbumNote] = Field(default_factory=list, max_length=30)


def apply_preferences(sheets, project, width, height):
    from .scene import Sheet, wrap
    preferences = DocumentPreferences.model_validate(project.properties.get("document_preferences", {}))
    replacements = {r.from_title: r.to_title for r in preferences.title_replacements}
    for sheet in sheets:
        if sheet.title in replacements:
            sheet.title = replacements[sheet.title]
    for note in preferences.notes:
        # User annotations do not alter detail dimensions or erase source evidence.
        sheet = None
        y = 34
        paragraphs = [part[i:i+4000] for part in note.text.split("\n") for i in range(0, max(1, len(part)), 4000)]
        for paragraph in paragraphs:
            for line in wrap(paragraph, width - 50, 3):
                if sheet is None or y > height - 70:
                    sheet = Sheet(note.title, "project_note", width=width, height=height)
                    sheets.append(sheet)
                    y = 37
                sheet.text(25, y, line, 3)
                y += 4.8
            y += 2
