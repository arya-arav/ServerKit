from collections import defaultdict
import threading

import pytest
from flask import Flask

from app import db
from app.models.security_alert import SecurityAlert
from app.services.anomaly_detection_service import AnomalyDetectionService


@pytest.mark.parametrize('status', ['open', 'acknowledged', 'resolved'])
def test_known_ip_survives_memory_reset(status):
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.session.add(SecurityAlert(alert_type='new_ip', source_ip='192.0.2.1', status=status))
        db.session.commit()
        # Avoid starting another singleton/background thread for this test.
        detector = object.__new__(AnomalyDetectionService)
        detector._lock = threading.Lock()
        detector._known_ips = defaultdict(set)
        assert detector.check_new_ip(None, '192.0.2.1') is False
        assert detector.check_new_ip(None, '192.0.2.2') is True
        assert detector.check_new_ip(None, '192.0.2.2') is False
        assert SecurityAlert.query.filter_by(alert_type='new_ip').count() == 2
        db.session.remove()
        db.drop_all()
