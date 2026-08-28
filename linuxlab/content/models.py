from typing import ClassVar

from wagtail.admin.panels import FieldPanel
from wagtail.fields import StreamField
from wagtail.models import Page

from linuxlab.content.blocks import LessonStreamBlock
from linuxlab.core.models import StableIdentityModel


class StableContentPage(StableIdentityModel, Page):
    """Abstract LinuxLab content page base with a stable cross-instance identity.

    public_id (from StableIdentityModel) is the stable UUID used for
    Central<->Node exchange (ADR-007, ADR-011). Wagtail's local integer PK
    and translation_key remain Wagtail-internal concerns and must never be
    reused as the distributed identity.
    """

    class Meta:
        abstract = True


class LearningPathPage(StableContentPage):
    parent_page_types: ClassVar[list[str]] = ["wagtailcore.Page"]
    subpage_types: ClassVar[list[str]] = ["content.ModulePage"]


class ModulePage(StableContentPage):
    parent_page_types: ClassVar[list[str]] = ["content.LearningPathPage"]
    subpage_types: ClassVar[list[str]] = ["content.LessonPage"]


class LessonPage(StableContentPage):
    parent_page_types: ClassVar[list[str]] = ["content.ModulePage"]
    subpage_types: ClassVar[list[str]] = []

    body = StreamField(LessonStreamBlock(), blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("body"),
    ]
