"""Zero-downtime by default: new apps move onto A/B slots by themselves.

Every app created from now on is marked ``slots_auto``; its deploys turn slots
on once it has a live release and qualifies, and say in the log why not when it
does not. Apps that already existed, apps that opted out, and a panel with the
switch off keep deploying in place.
"""
from app import db
from app.models.app_slot import AppSlot
from app.models.deployment import Deployment
from app.models.system_settings import SystemSettings
from app.services import deploy_settings
from app.services.deployment_service import DeploymentService
from app.services.slot_deploy_service import SlotDeployService

from factories import make_application
from test_plan87_slot_deploys import _live_app, world  # noqa: F401 - pytest fixture


def test_every_new_app_is_marked_for_auto_slots(app):
    row = make_application(db, name='fresh', app_type='docker')
    assert deploy_settings.get(row, 'slots_auto') is True
    assert row.slot_deploys_enabled is False, 'marked, not switched on: nothing is live yet'


def test_an_app_that_existed_before_keeps_deploying_in_place(app, world):
    row = _live_app(world)
    deploy_settings.set_internal(row, 'slots_auto', False)   # what older rows look like
    db.session.commit()
    assert SlotDeployService.auto_enable(row) is False
    assert row.slot_deploys_enabled is False


def test_the_first_deploy_goes_in_place_and_says_so(app, world):
    row = _live_app(world)
    Deployment.query.filter_by(app_id=row.id).delete()
    db.session.commit()
    log = []
    assert SlotDeployService.auto_enable(row, log=log.append) is False
    assert row.slot_deploys_enabled is False
    assert any('first deploy goes in place' in line for line in log)


def test_a_qualifying_app_deploys_through_slots_without_an_outage(app, world):
    row = _live_app(world)

    result = DeploymentService.deploy(row.id)

    assert result['success'], result
    assert row.slot_deploys_enabled is True and row.active_slot == 'b'
    b = AppSlot.query.filter_by(application_id=row.id, slot='b').one()
    assert b.state == 'live' and world.vhost_port == b.host_port
    assert world.outage() == [], 'the old release served until the new one was healthy'


def test_an_app_that_does_not_qualify_deploys_in_place_and_the_log_says_why(app, world):
    row = _live_app(world)
    db.session.delete(row.domains[0])
    db.session.commit()
    log = []
    assert SlotDeployService.auto_enable(row, log=log.append) is False
    assert row.slot_deploys_enabled is False
    assert any(line.startswith('Zero-downtime: deploying in place because') and 'domain' in line
               for line in log)


def test_opting_out_sticks(app, world):
    row = _live_app(world)
    assert SlotDeployService.auto_enable(row) is True
    assert SlotDeployService.set_enabled(row, False)['success']
    assert deploy_settings.get(row, 'slots_auto') is False
    assert SlotDeployService.auto_enable(row) is False
    assert row.slot_deploys_enabled is False


def test_the_panel_wide_switch_turns_it_off(app, world):
    row = _live_app(world)
    SystemSettings.set(SlotDeployService.AUTO_SETTING, False, value_type='boolean')
    db.session.commit()
    assert SlotDeployService.auto_enable(row) is False
    assert row.slot_deploys_enabled is False
