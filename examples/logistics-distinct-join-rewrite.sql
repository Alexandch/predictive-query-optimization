SELECT DISTINCT
    s.shipment_id,
    s.tracking_code
FROM logistics.shipments AS s
JOIN logistics.tracking_events AS te
    ON te.shipment_id = s.shipment_id
WHERE te.event_type = 'exception'
  AND s.shipment_id BETWEEN 1 AND 10000
ORDER BY s.shipment_id;
