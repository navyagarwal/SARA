from __future__ import annotations

from db import connect, reset_database, utc_now

DEMO_TABLES = [
    "agent_actions",
    "email_outbox",
    "human_tickets",
    "audit_logs",
    "tickets",
    "module_access",
    "refunds",
    "certificates",
    "payments",
    "course_progress",
    "enrollments",
    "modules",
    "students",
    "courses",
]


def seed() -> None:
    reset_database()
    reset_demo_data()


def reset_demo_data() -> None:
    now = utc_now()
    with connect() as connection:
        for table in DEMO_TABLES:
            connection.execute(f"DELETE FROM {table}")
        connection.execute("DELETE FROM sqlite_sequence")
        course_id = connection.execute(
            """
            INSERT INTO courses (
              course_code, title, certificate_required_progress, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            ("ai-productivity-101", "AI Productivity 101", 100, now),
        ).lastrowid

        modules = [
            (1, "Getting Started", None),
            (2, "Prompting Basics", 1),
            (3, "Building AI Workflows", 2),
        ]
        for module_number, title, prerequisite in modules:
            connection.execute(
                """
                INSERT INTO modules (
                  course_id, module_number, title, prerequisite_module_number
                ) VALUES (?, ?, ?, ?)
                """,
                (course_id, module_number, title, prerequisite),
            )

        students = [
            {
                "email": "maya@example.com",
                "name": "Maya Rao",
                "progress": 100,
                "last_module": 3,
                "certificate_status": "failed",
                "retry_count": 0,
                "certificate_error": "PDF_RENDER_TIMEOUT",
                "module_3_locked": 0,
                "module_3_reason": None,
            },
            {
                "email": "riya@example.com",
                "name": "Riya Shah",
                "progress": 45,
                "last_module": 1,
                "certificate_status": "not_eligible",
                "retry_count": 0,
                "certificate_error": None,
                "module_3_locked": 1,
                "module_3_reason": "prerequisite_incomplete",
            },
            {
                "email": "arjun@example.com",
                "name": "Arjun Mehta",
                "progress": 40,
                "last_module": 2,
                "certificate_status": "not_eligible",
                "retry_count": 0,
                "certificate_error": None,
                "module_3_locked": 1,
                "module_3_reason": "prerequisite_incomplete",
            },
        ]

        for student in students:
            student_id = connection.execute(
                """
                INSERT INTO students (email, name, support_tier, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (student["email"], student["name"], "standard", now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO enrollments (student_id, course_id, status, enrolled_at)
                VALUES (?, ?, ?, ?)
                """,
                (student_id, course_id, "active", now),
            )
            connection.execute(
                """
                INSERT INTO course_progress (
                  student_id, course_id, progress_percent, last_completed_module, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (student_id, course_id, student["progress"], student["last_module"], now),
            )
            connection.execute(
                """
                INSERT INTO payments (
                  student_id, course_id, amount, currency, status, paid_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (student_id, course_id, 4999, "INR", "paid", now),
            )
            connection.execute(
                """
                INSERT INTO certificates (
                  student_id, course_id, status, retry_count, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    student_id,
                    course_id,
                    student["certificate_status"],
                    student["retry_count"],
                    student["certificate_error"],
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO refunds (student_id, course_id, status, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (student_id, course_id, "none", None, now),
            )
            for module_number in (1, 2, 3):
                is_locked = student["module_3_locked"] if module_number == 3 else 0
                reason = student["module_3_reason"] if module_number == 3 else None
                connection.execute(
                    """
                    INSERT INTO module_access (
                      student_id, course_id, module_number, is_locked, reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (student_id, course_id, module_number, is_locked, reason, now),
                )


if __name__ == "__main__":
    seed()
    print("Seeded data/support_agent.db")
