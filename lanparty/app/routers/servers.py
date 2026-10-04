import re

from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session, select

from .. import gameservers as gs
from ..auth import require_admin
from ..db import active_event, get_session
from ..models import GameServer
from ..web import back, game_host, render

router = APIRouter(dependencies=[Depends(require_admin)])


def server_rows(s: Session, event_id: int) -> list[dict]:
    tpls = gs.templates()
    rows = []
    for g in s.exec(select(GameServer).where(GameServer.event_id == event_id).order_by(GameServer.id)).all():
        state, detail = gs.status(g.container_name)
        tpl = tpls.get(g.template, {})
        rows.append(dict(g=g, state=state, detail=detail, label=tpl.get("label", g.template),
                         ports=sorted(gs.host_ports(tpl, g.host_port)) if tpl else [g.host_port]))
    return rows


@router.get("/servers")
def page(request: Request, s: Session = Depends(get_session)):
    ev = active_event(s)
    ok, err = gs.docker_available()
    return render(request, "servers.html", event=ev, section="servers", docker_ok=ok, docker_err=err,
                  rows=server_rows(s, ev.id) if ok else [], templates_=gs.templates(), host=game_host(request))


@router.get("/servers/rows")
def rows(request: Request, s: Session = Depends(get_session)):
    ev = active_event(s)
    return render(request, "_server_rows.html", rows=server_rows(s, ev.id), host=game_host(request))


@router.post("/servers")
def create(name: str = Form(...), template: str = Form(...), host_port: int = Form(0), s: Session = Depends(get_session)):
    ev = active_event(s)
    tpls = gs.templates()
    if template not in tpls:
        return back("/servers", "Pick a game from the list.")
    tpl = tpls[template]
    port = host_port or int(tpl["ports"][0][0])
    wanted = gs.host_ports(tpl, port)
    for other in s.exec(select(GameServer)).all():
        otpl = tpls.get(other.template)
        taken = gs.host_ports(otpl, other.host_port) if otpl else {other.host_port}
        clash = wanted & taken
        if clash:
            return back("/servers", f"Port {min(clash)} is already used by {other.name}. Choose a different host port.")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or template
    g = GameServer(event_id=ev.id, name=name.strip() or tpl["label"], template=template, host_port=port,
                   container_name=f"lan-{slug}-{port}")
    s.add(g)
    s.commit()
    gs.create(g.name, g.container_name, template, port)
    return back("/servers")


@router.post("/servers/{sid}/{verb}")
def act(request: Request, sid: int, verb: str, s: Session = Depends(get_session)):
    g = s.get(GameServer, sid)
    if not g:
        return back("/servers")
    if verb == "delete":
        gs.remove(g.container_name)
        s.delete(g)
        s.commit()
        return back("/servers")
    if verb in {"start", "stop", "restart"}:
        try:
            gs.action(g.container_name, verb)
        except Exception as e:  # noqa: BLE001
            return back("/servers", f"Couldn't {verb} {g.name}: {e}")
    return back("/servers")


@router.get("/servers/{sid}/logs")
def logs(request: Request, sid: int, s: Session = Depends(get_session)):
    g = s.get(GameServer, sid)
    return render(request, "_server_logs.html", g=g, text=gs.logs(g.container_name) if g else "")
