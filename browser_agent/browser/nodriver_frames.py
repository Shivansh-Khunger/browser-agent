# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Reconcile Chromium's frame tree with owning nodriver CDP sessions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from nodriver import cdp
from nodriver.cdp.page import FrameId, FrameTree
from nodriver.cdp.target import TargetInfo
from nodriver.core.tab import IFrame, Tab


@dataclass(frozen=True, slots=True)
class FrameContext:
    frame_id: FrameId
    session: Tab
    document_generation: int
    breadcrumb: tuple[str, ...]
    origin: str | None
    children: tuple[FrameContext, ...]
    session_root: bool = False

    def with_breadcrumb(self, breadcrumb: tuple[str, ...]) -> FrameContext:
        """Replace this breadcrumb and its prefix throughout descendants."""
        old_prefix = self.breadcrumb

        def rebase(frame: FrameContext) -> FrameContext:
            suffix = frame.breadcrumb[len(old_prefix) :]
            return replace(
                frame,
                breadcrumb=(*breadcrumb, *suffix),
                children=tuple(rebase(child) for child in frame.children),
            )

        return rebase(self)


class FrameRegistry:
    """Keep OOPIF sessions stable while deriving order from Page.getFrameTree."""

    def __init__(self, main_tab: Tab) -> None:
        self._main_tab = main_tab
        self._oopif_sessions: dict[str, IFrame] = {}
        self._generations: dict[str, tuple[str, int]] = {}

    async def reconcile(self) -> FrameContext:
        frame_tree = await self._main_tab.send(cdp.page.get_frame_tree())
        targets = await self._main_tab.send(cdp.target.get_targets())
        iframe_targets = {
            str(target.target_id): target for target in targets if target.type_ == "iframe"
        }
        live_ids = set(iframe_targets)
        close_errors: list[Exception] = []
        for frame_id, session in tuple(self._oopif_sessions.items()):
            if frame_id not in live_ids:
                try:
                    await session.aclose()
                except Exception as error:
                    close_errors.append(error)
                else:
                    del self._oopif_sessions[frame_id]
        if close_errors:
            raise ExceptionGroup("failed to close detached frame sessions", close_errors)
        for frame_id, target in iframe_targets.items():
            if frame_id not in self._oopif_sessions:
                self._oopif_sessions[frame_id] = IFrame(target=target, parent=self._main_tab)
            else:
                self._oopif_sessions[frame_id].target = target
        oopif_loaders: dict[str, str] = {}
        for frame_id, session in self._oopif_sessions.items():
            try:
                target_tree = await session.send(cdp.page.get_frame_tree())
            except Exception:
                oopif_loaders[frame_id] = iframe_targets[frame_id].url
            else:
                oopif_loaders[frame_id] = str(target_tree.frame.loader_id)

        seen: set[str] = set()

        def tree_ids(tree: FrameTree) -> set[str]:
            return {str(tree.frame.id_)} | {
                frame_id for child in tree.child_frames or () for frame_id in tree_ids(child)
            }

        native_frame_ids = tree_ids(frame_tree)
        synthetic_by_parent: dict[str, list[TargetInfo]] = {}
        for target in iframe_targets.values():
            if str(target.target_id) in native_frame_ids or target.parent_frame_id is None:
                continue
            synthetic_by_parent.setdefault(str(target.parent_frame_id), []).append(target)
        for children in synthetic_by_parent.values():
            children.sort(key=lambda target: str(target.target_id))

        def generation(frame_id: str, loader: str) -> int:
            prior_loader, prior_generation = self._generations.get(frame_id, ("", 0))
            value = prior_generation if loader == prior_loader else prior_generation + 1
            self._generations[frame_id] = (loader, value)
            seen.add(frame_id)
            return value

        def synthetic(
            target: TargetInfo,
            breadcrumb: tuple[str, ...],
            sibling_index: int,
        ) -> FrameContext:
            target_id = str(target.target_id)
            session = self._oopif_sessions[target_id]
            label = f"frame-{sibling_index}"
            origin = _safe_origin(target.url)
            children = tuple(
                synthetic(child, (*breadcrumb, label), index)
                for index, child in enumerate(synthetic_by_parent.get(target_id, ()), start=1)
            )
            return FrameContext(
                frame_id=cdp.page.FrameId(target_id),
                session=session,
                document_generation=generation(target_id, oopif_loaders[target_id]),
                breadcrumb=(*breadcrumb, label),
                origin=origin,
                children=children,
                session_root=True,
            )

        def build(
            tree: FrameTree,
            breadcrumb: tuple[str, ...],
            sibling_index: int,
            inherited_session: Tab,
        ) -> FrameContext:
            frame = tree.frame
            frame_id = str(frame.id_)
            loader = str(frame.loader_id)
            document_generation = generation(frame_id, loader)
            if frame.parent_id is None:
                own_breadcrumb = ("main",)
            else:
                label = (
                    frame.name.strip()
                    if frame.name and frame.name.strip()
                    else f"frame-{sibling_index}"
                )
                own_breadcrumb = (*breadcrumb, label)
            oopif_session = self._oopif_sessions.get(frame_id)
            session = oopif_session or inherited_session
            native_children = tuple(
                build(child, own_breadcrumb, index, session)
                for index, child in enumerate(tree.child_frames or (), start=1)
            )
            synthetic_children = tuple(
                synthetic(child, own_breadcrumb, len(native_children) + index)
                for index, child in enumerate(synthetic_by_parent.get(frame_id, ()), start=1)
            )
            origin = (
                frame.security_origin
                if frame.security_origin and frame.security_origin != "://"
                else None
            )
            return FrameContext(
                frame_id=frame.id_,
                session=session,
                document_generation=document_generation,
                breadcrumb=own_breadcrumb,
                origin=origin,
                children=(*native_children, *synthetic_children),
                session_root=oopif_session is not None,
            )

        root = build(frame_tree, (), 1, self._main_tab)
        self._generations = {
            frame_id: value for frame_id, value in self._generations.items() if frame_id in seen
        }
        return root

    async def close(self) -> None:
        close_errors: list[Exception] = []
        for frame_id, session in tuple(self._oopif_sessions.items()):
            try:
                await session.aclose()
            except Exception as error:
                close_errors.append(error)
            else:
                del self._oopif_sessions[frame_id]
        if close_errors:
            raise ExceptionGroup("failed to close frame sessions", close_errors)


def find_frame(root: FrameContext, frame_id: str) -> FrameContext | None:
    if str(root.frame_id) == frame_id:
        return root
    for child in root.children:
        if match := find_frame(child, frame_id):
            return match
    return None


def frame_generations(root: FrameContext) -> dict[str, int]:
    generations = {str(root.frame_id): root.document_generation}
    for child in root.children:
        generations.update(frame_generations(child))
    return generations


def _safe_origin(url: str) -> str | None:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return None
    return f"{parsed.scheme}://{host}{port}"
