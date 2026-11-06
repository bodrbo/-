-- Выполненные задачи расписания тюнинга и их выплаты в «Зарплатах». Только чтение.
--
-- Запуск на сервере (из папки с базой workhours.db):
--     sqlite3 -readonly workhours.db < app/scripts/diagnose_schedule_payouts.sql
--
-- Показывает: сколько задач стоит «Выполнена», у скольких есть выплата, а у
-- остальных — почему её нет (нулевая ставка/часы, администратор на окладе,
-- подрядчик, выплата удалена в «Зарплатах» или просто не создана), и куда
-- попали выплаты последних дней (они датируются днём, когда задачу отметили
-- выполненной, а не днём самой задачи).

.headers on
.mode list
.separator " | "

CREATE TEMP VIEW done_tasks AS
SELECT
    'по работе' AS kind,
    t.id AS task_id,
    tia.employee_name AS employee,
    COALESCE(NULLIF(t.title, ''), ti.work_name) AS title,
    tia.rate AS rate,
    tia.norm_hours AS hours,
    tia.rate * tia.norm_hours AS amount,
    tia.entry_id AS entry_id,
    EXISTS (SELECT 1 FROM entries e WHERE e.id = tia.entry_id) AS entry_exists,
    tia.partner_id IS NOT NULL AS is_partner,
    EXISTS (
        SELECT 1 FROM employee_positions p JOIN employees emp ON emp.id = p.employee_id
        WHERE emp.name = tia.employee_name AND emp.deleted_at IS NULL AND p.position = 'Администратор'
    ) AS is_admin,
    (SELECT MIN(d.work_date) FROM tuning_schedule_task_days d WHERE d.task_id = t.id) AS task_date
FROM tuning_item_assignments tia
JOIN tuning_schedule_tasks t ON t.assignment_id = tia.id
LEFT JOIN tuning_order_items ti ON ti.id = tia.item_id
WHERE tia.assignment_status = 'done'
UNION ALL
SELECT
    'свободная',
    t.id,
    t.employee_name,
    t.title,
    t.rate,
    COALESCE((SELECT SUM(d.planned_hours) FROM tuning_schedule_task_days d WHERE d.task_id = t.id), 0),
    t.rate * COALESCE((SELECT SUM(d.planned_hours) FROM tuning_schedule_task_days d WHERE d.task_id = t.id), 0),
    t.entry_id,
    EXISTS (SELECT 1 FROM entries e WHERE e.id = t.entry_id),
    0,
    EXISTS (
        SELECT 1 FROM employee_positions p JOIN employees emp ON emp.id = p.employee_id
        WHERE emp.name = t.employee_name AND emp.deleted_at IS NULL AND p.position = 'Администратор'
    ),
    (SELECT MIN(d.work_date) FROM tuning_schedule_task_days d WHERE d.task_id = t.id)
FROM tuning_schedule_tasks t
WHERE t.assignment_id IS NULL AND t.status = 'done';

CREATE TEMP VIEW done_tasks_checked AS
SELECT *,
    CASE
        WHEN is_partner THEN 'подрядчик (платится не через Зарплаты)'
        WHEN entry_id IS NOT NULL AND entry_exists THEN 'выплата есть'
        WHEN amount <= 0 AND is_admin THEN 'администратор на окладе (выплата не нужна)'
        WHEN amount <= 0 THEN 'нулевая ставка или нулевые часы'
        WHEN entry_id IS NOT NULL AND NOT entry_exists THEN 'выплата была, но удалена в «Зарплатах»'
        ELSE 'ВЫПЛАТА НЕ СОЗДАНА'
    END AS reason
FROM done_tasks;

SELECT '=== 1. Выполненные задачи расписания по причинам ===' AS "";
SELECT kind AS "вид", reason AS "итог", COUNT(*) AS "задач", ROUND(SUM(amount), 2) AS "сумма, ₽"
FROM done_tasks_checked GROUP BY kind, reason ORDER BY kind, reason;

SELECT '' AS "";
SELECT '=== 2. Выполнены, но выплата должна быть и её нет (до 80 строк) ===' AS "";
SELECT kind AS "вид", task_id AS "задача", employee AS "сотрудник", title AS "название",
       rate AS "ставка", ROUND(hours, 2) AS "часы", ROUND(amount, 2) AS "сумма", task_date AS "дата задачи", reason AS "причина"
FROM done_tasks_checked
WHERE reason IN ('ВЫПЛАТА НЕ СОЗДАНА', 'выплата была, но удалена в «Зарплатах»', 'нулевая ставка или нулевые часы')
ORDER BY task_date DESC, task_id DESC LIMIT 80;

SELECT '' AS "";
SELECT '=== 3. Участники свободных задач (ещё сотрудники в задаче) без выплаты ===' AS "";
SELECT t.id AS "задача", m.employee_name AS "сотрудник", t.title AS "название", m.rate AS "ставка", m.hours AS "часы",
       CASE WHEN m.entry_id IS NULL THEN 'нет выплаты'
            WHEN EXISTS (SELECT 1 FROM entries e WHERE e.id = m.entry_id) THEN 'есть'
            ELSE 'удалена' END AS "выплата"
FROM tuning_schedule_task_members m JOIN tuning_schedule_tasks t ON t.id = m.task_id
WHERE t.assignment_id IS NULL AND t.status = 'done'
  AND (m.entry_id IS NULL OR NOT EXISTS (SELECT 1 FROM entries e WHERE e.id = m.entry_id))
ORDER BY t.id DESC LIMIT 40;

SELECT '' AS "";
SELECT '=== 4. Статусы всех задач расписания, созданных за последние 30 дней ===' AS "";
SELECT CASE WHEN t.assignment_id IS NULL THEN 'свободная' ELSE 'по работе' END AS "вид",
       COALESCE(tia.assignment_status, t.status) AS "статус", COUNT(*) AS "задач"
FROM tuning_schedule_tasks t LEFT JOIN tuning_item_assignments tia ON tia.id = t.assignment_id
WHERE t.created_at >= date('now', '-30 day')
GROUP BY 1, 2 ORDER BY 1, 2;

SELECT '' AS "";
SELECT '=== 5. Выплаты за последние 14 дней (когда созданы и каким днём датированы) ===' AS "";
SELECT substr(created_at, 1, 10) AS "создана", work_date AS "дата выплаты", COUNT(*) AS "записей", ROUND(SUM(amount), 2) AS "сумма, ₽"
FROM entries WHERE created_at >= date('now', '-14 day')
GROUP BY 1, 2 ORDER BY 1 DESC, 2 DESC LIMIT 40;
