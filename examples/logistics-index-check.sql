SELECT tracking_event_id, shipment_id, event_type, event_time
FROM logistics.tracking_events
WHERE facility_id = 17
  AND event_type = 'exception'
ORDER BY event_time DESC
LIMIT 200;
