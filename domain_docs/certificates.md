# Certificate Domain Knowledge

Certificates are issued when:
- the learner has an active enrollment
- payment is paid
- course progress is at or above the course certificate requirement
- certificate generation succeeds

If the learner is eligible but certificate status is failed, the agent may queue a certificate generation retry.

The certificate retry_count is only the number of previous certificate generation attempts. Do not compare retry_count with course progress or certificate_required_progress.

When the learner has active enrollment, paid payment, required progress, and certificate status failed, the root cause is that certificate generation failed on the platform side.

The agent should not promise an exact delivery time unless the system has a timestamp or SLA.

Escalate when:
- payment is refunded, disputed, or pending
- certificate generation has failed more than twice
- student is VIP
- student progress and certificate records conflict
- the student is not found
