from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Region(Input):
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def contained(self):
        if self.x + self.w > 1.0000001 or self.y + self.h > 1.0000001:
            raise ValueError("A região deve caber no vídeo.")
        return self


class Processing(Input):
    min_seconds: int = Field(default=20, ge=5, le=60)
    max_seconds: int = Field(default=60, ge=5, le=90)
    limit: int = Field(default=6, ge=1, le=20)
    language: Literal["auto", "pt", "en"] = "auto"
    vocabulary: str = Field(default="", max_length=500)
    webcam: Region | None = None
    detect_webcam: bool = True
    audio_stream: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def range_order(self):
        if self.min_seconds > self.max_seconds:
            raise ValueError("A duração mínima deve ser menor ou igual à máxima.")
        return self


class Captions(Input):
    font: Literal["DejaVu Sans", "Liberation Sans", "Noto Sans"] = "DejaVu Sans"
    size: int = Field(default=62, ge=28, le=95)
    color: str = Field(default="#FFFFFF", pattern=r"^#[a-fA-F0-9]{6}$")
    highlight: str = Field(default="#C7FF61", pattern=r"^#[a-fA-F0-9]{6}$")
    words: int = Field(default=5, ge=2, le=8)
    position: Literal["safe", "bottom"] = "safe"
    animate: bool = True


class Correction(Input):
    id: int = Field(ge=0)
    text: str = Field(max_length=1200)


Tag = Annotated[str, Field(min_length=1, max_length=55)]


class Metadata(Input):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    hashtags: list[Tag] = Field(default_factory=list, max_length=15)
    hook: str = Field(default="", max_length=180)
    caption: str = Field(default="", max_length=2000)


class Edit(Metadata):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    layout: Literal["auto", "gameplay", "webcam", "split"] = "auto"
    captions: Captions = Field(default_factory=Captions)
    corrections: list[Correction] = Field(default_factory=list, max_length=150)


class Review(Input):
    decision: Literal["APPROVED", "REJECTED"]
    revision: int = Field(ge=1)


class Publish(Metadata):
    account_id: str
    revision: int = Field(ge=1)
    due: float | None = None
    privacy: str = Field(default="", max_length=40)
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    commercial: bool = False
    own_brand: bool = False
    branded_content: bool = False
    consent: bool


class Metrics(Input):
    views: int | None = Field(default=None, ge=0, le=10**15)
    likes: int | None = Field(default=None, ge=0, le=10**15)
    comments: int | None = Field(default=None, ge=0, le=10**15)
    shares: int | None = Field(default=None, ge=0, le=10**15)
    retention: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def nonempty(self):
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("Informe pelo menos uma métrica.")
        return self
