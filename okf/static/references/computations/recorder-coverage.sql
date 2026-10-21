-- Share of minutes the recorder captured, per New York day between two dates
-- (inclusive). The first and last days can be partial.
SELECT
    CAST(timezone('America/New_York', minute) AS DATE) AS local_date,
    count(*) AS minutes_in_window,
    count(*) FILTER (WHERE recorder_up) AS minutes_recorded,
    round(count(*) FILTER (WHERE recorder_up) / count(*), 4) AS coverage
FROM fct_recorder_minute
WHERE CAST(timezone('America/New_York', minute) AS DATE)
      BETWEEN CAST($start_date AS DATE) AND CAST($end_date AS DATE)
GROUP BY 1
ORDER BY 1
