// Main interactive JavaScript for Shift & OT Payroll HRMS

document.addEventListener('DOMContentLoaded', function () {
    // 1. Live header clock
    const clockElement = document.getElementById('liveClock');
    if (clockElement) {
        function updateClock() {
            const now = new Date();
            clockElement.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }
        updateClock();
        setInterval(updateClock, 1000);
    }

    // 2. Modal open/close controls
    window.openModal = function (modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            document.body.style.overflow = 'hidden';
        }
    };

    window.closeModal = function (modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.add('hidden');
            modal.classList.remove('flex');
            document.body.style.overflow = 'auto';
        }
    };

    // Close on backdrop click
    document.querySelectorAll('.modal-container').forEach(modal => {
        modal.addEventListener('click', function (e) {
            if (e.target === modal) {
                closeModal(modal.id);
            }
        });
    });

    // 3. Quick Punch Simulator handler
    const quickPunchForm = document.getElementById('quickPunchForm');
    if (quickPunchForm) {
        window.submitQuickPunch = function (punchType) {
            const empSelect = document.getElementById('quickPunchEmp');
            const empId = empSelect.value;
            if (!empId) {
                alert('Please select an employee to punch.');
                return;
            }

            const feedbackDiv = document.getElementById('quickPunchFeedback');
            feedbackDiv.innerHTML = '<span class="text-blue-600 animate-pulse"><i class="fas fa-spinner fa-spin mr-1"></i> Processing punch...</span>';
            feedbackDiv.classList.remove('hidden');

            fetch('/api/punch-now', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    employee_id: parseInt(empId),
                    punch_type: punchType
                })
            })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    feedbackDiv.innerHTML = `
                        <div class="p-2.5 bg-emerald-50 border border-emerald-200 text-emerald-800 rounded-lg text-xs">
                            <i class="fas fa-check-circle mr-1 text-emerald-600"></i> ${data.message} 
                            <span class="font-semibold ml-2">Status: ${data.status}</span> | 
                            <span>Hours: ${data.work_hours}h</span>
                            ${data.ot_hours > 0 ? `<span class="text-amber-700 font-bold ml-1">(OT: +${data.ot_hours}h)</span>` : ''}
                        </div>
                    `;
                    setTimeout(() => {
                        window.location.reload();
                    }, 1800);
                } else {
                    feedbackDiv.innerHTML = `<span class="text-red-600"><i class="fas fa-exclamation-triangle mr-1"></i> ${data.error || 'Failed'}</span>`;
                }
            })
            .catch(err => {
                feedbackDiv.innerHTML = `<span class="text-red-600">Error connecting to server.</span>`;
            });
        };
    }
});
