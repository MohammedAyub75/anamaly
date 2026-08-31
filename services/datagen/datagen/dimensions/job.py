"""`dim_job` -- roughly 1,200 job codes across the 11 job families.

Built by enumeration rather than sampling: the catalogue of jobs a company has
is a fact about the company, not a random draw, and enumerating it means the
dimension is identical at every scale and under every seed.  That in turn makes
`job_family` a stable peer-cohort key.

Three columns here are anomaly inputs and are therefore not decoration:
`min_grade`/`max_grade` are what A08 compares an employee's grade against,
`min_education` and `required_certifications` are what A11 tests, and
`safety_critical` both gates ON_CALL / SAFETY / CERT_PREMIUM and decides whether
an A11 finding is CRITICAL or MEDIUM.
"""

from __future__ import annotations

from typing import Any

# (english, arabic, safety_critical) per family.
FAMILIES: dict[str, tuple[str, tuple[tuple[str, str, bool], ...]]] = {
    "Drilling": (
        "DRL",
        (
            ("Drilling Engineer", "مهندس حفر", True),
            ("Rig Supervisor", "مشرف حفارة", True),
            ("Mud Engineer", "مهندس طين الحفر", True),
            ("Well Control Specialist", "أخصائي التحكم بالآبار", True),
            ("Directional Driller", "حفار اتجاهي", True),
            ("Wireline Operator", "مشغل الخط السلكي", True),
            ("Cementing Specialist", "أخصائي تسميت", True),
            ("Coiled Tubing Operator", "مشغل الأنابيب الملفوفة", True),
            ("Completion Engineer", "مهندس إكمال الآبار", True),
            ("Rig Mechanic", "ميكانيكي حفارة", True),
            ("Drilling Planner", "مخطط عمليات الحفر", False),
            ("Drilling Data Analyst", "محلل بيانات الحفر", False),
            ("Well Integrity Engineer", "مهندس سلامة الآبار", True),
            ("Drilling Fluids Technician", "فني سوائل الحفر", True),
            ("Rig Logistics Coordinator", "منسق لوجستيات الحفارة", False),
            ("Drilling Cost Controller", "مراقب تكاليف الحفر", False),
        ),
    ),
    "Reservoir": (
        "RSV",
        (
            ("Reservoir Engineer", "مهندس مكامن", False),
            ("Petrophysicist", "أخصائي فيزياء الصخور", False),
            ("Production Geologist", "جيولوجي إنتاج", False),
            ("Simulation Engineer", "مهندس محاكاة", False),
            ("Geophysicist", "جيوفيزيائي", False),
            ("Well Test Analyst", "محلل اختبار الآبار", False),
            ("Reserves Analyst", "محلل الاحتياطيات", False),
            ("Formation Evaluation Specialist", "أخصائي تقييم التكوينات", False),
            ("Enhanced Recovery Engineer", "مهندس الاستخلاص المعزز", False),
            ("Subsurface Data Manager", "مدير بيانات باطن الأرض", False),
            ("Core Analysis Technician", "فني تحليل العينات", False),
            ("Seismic Interpreter", "مفسر بيانات زلزالية", False),
        ),
    ),
    "Process Ops": (
        "OPS",
        (
            ("Process Operator", "مشغل عمليات", True),
            ("Panel Operator", "مشغل لوحة تحكم", True),
            ("Shift Supervisor", "مشرف مناوبة", True),
            ("Process Engineer", "مهندس عمليات", True),
            ("Gas Plant Operator", "مشغل معمل غاز", True),
            ("Refinery Operator", "مشغل مصفاة", True),
            ("Terminal Operator", "مشغل ميناء", True),
            ("Utilities Operator", "مشغل مرافق", True),
            ("Laboratory Technician", "فني مختبر", False),
            ("Production Coordinator", "منسق إنتاج", False),
            ("Process Control Engineer", "مهندس تحكم بالعمليات", True),
            ("Pipeline Controller", "مراقب خطوط الأنابيب", True),
            ("Tank Farm Operator", "مشغل حقل الخزانات", True),
            ("Operations Planner", "مخطط عمليات", False),
            ("Energy Efficiency Analyst", "محلل كفاءة الطاقة", False),
        ),
    ),
    "Maintenance": (
        "MNT",
        (
            ("Mechanical Technician", "فني ميكانيكي", True),
            ("Electrical Technician", "فني كهربائي", True),
            ("Instrument Technician", "فني أجهزة دقيقة", True),
            ("Rotating Equipment Engineer", "مهندس معدات دوارة", True),
            ("Reliability Engineer", "مهندس موثوقية", False),
            ("Inspection Engineer", "مهندس تفتيش", True),
            ("Welding Inspector", "مفتش لحام", True),
            ("Maintenance Planner", "مخطط صيانة", False),
            ("Corrosion Engineer", "مهندس تآكل", True),
            ("Turnaround Coordinator", "منسق الصيانة الشاملة", False),
            ("Valve Technician", "فني صمامات", True),
            ("Predictive Maintenance Analyst", "محلل الصيانة التنبؤية", False),
            ("Machinist", "مشغل مخرطة", True),
            ("Scaffolding Supervisor", "مشرف سقالات", True),
        ),
    ),
    "HSE": (
        "HSE",
        (
            ("Safety Officer", "مسؤول سلامة", True),
            ("Industrial Hygienist", "أخصائي صحة مهنية", True),
            ("Fire Protection Engineer", "مهندس حماية من الحريق", True),
            ("Environmental Specialist", "أخصائي بيئة", False),
            ("Emergency Response Coordinator", "منسق الاستجابة للطوارئ", True),
            ("Process Safety Engineer", "مهندس سلامة العمليات", True),
            ("HSE Auditor", "مدقق الصحة والسلامة", False),
            ("Occupational Health Advisor", "مستشار الصحة المهنية", False),
            ("Permit to Work Coordinator", "منسق تصاريح العمل", True),
            ("Risk Assessment Specialist", "أخصائي تقييم المخاطر", False),
            ("HSE Trainer", "مدرب الصحة والسلامة", False),
        ),
    ),
    "IT": (
        "ITS",
        (
            ("Systems Engineer", "مهندس أنظمة", False),
            ("Network Engineer", "مهندس شبكات", False),
            ("Application Developer", "مطور تطبيقات", False),
            ("Data Engineer", "مهندس بيانات", False),
            ("Cybersecurity Analyst", "محلل أمن سيبراني", False),
            ("SCADA Engineer", "مهندس أنظمة سكادا", True),
            ("Database Administrator", "مدير قواعد بيانات", False),
            ("IT Service Desk Analyst", "محلل دعم تقني", False),
            ("Cloud Infrastructure Engineer", "مهندس بنية سحابية", False),
            ("Business Systems Analyst", "محلل أنظمة الأعمال", False),
            ("Solution Architect", "مهندس حلول", False),
        ),
    ),
    "Finance": (
        "FIN",
        (
            ("Financial Analyst", "محلل مالي", False),
            ("Accountant", "محاسب", False),
            ("Cost Controller", "مراقب تكاليف", False),
            ("Treasury Analyst", "محلل خزينة", False),
            ("Internal Auditor", "مدقق داخلي", False),
            ("Tax Specialist", "أخصائي ضرائب", False),
            ("Budget Analyst", "محلل ميزانية", False),
            ("Payroll Analyst", "محلل رواتب", False),
            ("Accounts Payable Clerk", "موظف حسابات دائنة", False),
            ("Investment Analyst", "محلل استثمار", False),
            ("Financial Reporting Specialist", "أخصائي تقارير مالية", False),
        ),
    ),
    "HR": (
        "HRS",
        (
            ("HR Business Partner", "شريك أعمال الموارد البشرية", False),
            ("Compensation Analyst", "محلل التعويضات", False),
            ("Recruitment Specialist", "أخصائي توظيف", False),
            ("Learning and Development Advisor", "مستشار التدريب والتطوير", False),
            ("Employee Relations Officer", "مسؤول علاقات الموظفين", False),
            ("HR Operations Coordinator", "منسق عمليات الموارد البشرية", False),
            ("Saudization Programme Officer", "مسؤول برنامج التوطين", False),
            ("Talent Management Specialist", "أخصائي إدارة المواهب", False),
            ("HR Data Analyst", "محلل بيانات الموارد البشرية", False),
            ("Organisational Development Advisor", "مستشار التطوير المؤسسي", False),
        ),
    ),
    "Procurement": (
        "PRC",
        (
            ("Buyer", "مشتري", False),
            ("Contracts Specialist", "أخصائي عقود", False),
            ("Category Manager", "مدير فئة مشتريات", False),
            ("Supplier Quality Engineer", "مهندس جودة الموردين", False),
            ("Materials Coordinator", "منسق مواد", False),
            ("Warehouse Supervisor", "مشرف مستودع", False),
            ("Logistics Coordinator", "منسق لوجستيات", False),
            ("Local Content Analyst", "محلل المحتوى المحلي", False),
            ("Expediting Officer", "مسؤول متابعة التوريد", False),
            ("Inventory Analyst", "محلل مخزون", False),
        ),
    ),
    "Medical": (
        "MED",
        (
            ("Occupational Physician", "طبيب مهني", True),
            ("Registered Nurse", "ممرض مسجل", True),
            ("Paramedic", "مسعف", True),
            ("Radiographer", "فني أشعة", True),
            ("Pharmacist", "صيدلي", True),
            ("Laboratory Scientist", "أخصائي مختبر طبي", True),
            ("Physiotherapist", "أخصائي علاج طبيعي", False),
            ("Medical Records Officer", "مسؤول السجلات الطبية", False),
            ("Clinic Coordinator", "منسق عيادة", False),
        ),
    ),
    "Security": (
        "SEC",
        (
            ("Security Officer", "ضابط أمن", True),
            ("Access Control Specialist", "أخصائي التحكم بالدخول", True),
            ("Industrial Security Advisor", "مستشار الأمن الصناعي", True),
            ("Investigations Officer", "مسؤول تحقيقات", False),
            ("Security Systems Technician", "فني أنظمة أمنية", True),
            ("Loss Prevention Analyst", "محلل منع الخسائر", False),
            ("Emergency Control Room Operator", "مشغل غرفة تحكم الطوارئ", True),
            ("Security Planner", "مخطط أمني", False),
        ),
    ),
}

# (english prefix, arabic prefix, min_grade, max_grade, min_education)
SENIORITY: tuple[tuple[str, str, int, int, str], ...] = (
    ("Trainee", "متدرب", 1, 3, "secondary"),
    ("Assistant", "مساعد", 1, 4, "secondary"),
    ("Junior", "مبتدئ", 3, 6, "secondary"),
    ("", "", 5, 9, "diploma"),
    ("Senior", "أول", 8, 12, "bachelor"),
    ("Lead", "رئيس", 11, 15, "bachelor"),
    ("Expert", "خبير", 13, 16, "master"),
    ("Principal", "استشاري", 14, 17, "master"),
    ("Chief", "كبير", 17, 20, "master"),
)

# Certifications a safety-critical post requires, by family.
CERTIFICATIONS: dict[str, tuple[str, ...]] = {
    "Drilling": ("IWCF-L2", "H2S-AWARE", "RIG-PASS"),
    "Reservoir": ("SPE-CERT",),
    "Process Ops": ("PROC-SAFE", "H2S-AWARE", "CONF-SPACE"),
    "Maintenance": ("API-570", "HOT-WORK", "LOTO"),
    "HSE": ("NEBOSH-IGC", "FIRST-AID", "PTW-AUTH"),
    "IT": ("ICS-SEC",),
    "Finance": (),
    "HR": (),
    "Procurement": (),
    "Medical": ("BLS", "ACLS"),
    "Security": ("SEC-LIC", "FIRST-AID"),
}


def build() -> dict[str, Any]:
    codes: list[str] = []
    titles_en: list[str] = []
    titles_ar: list[str] = []
    families: list[str] = []
    min_grade: list[int] = []
    max_grade: list[int] = []
    min_education: list[str] = []
    required: list[list[str]] = []
    safety: list[bool] = []

    for family, (prefix, roles) in FAMILIES.items():
        serial = 0
        for role_en, role_ar, is_safety in roles:
            for level_en, level_ar, low, high, education in SENIORITY:
                serial += 1
                codes.append(f"{prefix}-{serial:04d}")
                titles_en.append(f"{level_en} {role_en}".strip())
                titles_ar.append(f"{role_ar} {level_ar}".strip())
                families.append(family)
                min_grade.append(low)
                max_grade.append(high)
                min_education.append(education)
                safety.append(is_safety)
                pool = CERTIFICATIONS[family]
                # A senior safety-critical post carries the full set; a junior
                # one carries the first certification only.
                if is_safety and pool:
                    required.append(list(pool) if low >= 5 else [pool[0]])
                else:
                    required.append([])

    return {
        "job_code": codes,
        "job_title_en": titles_en,
        "job_title_ar": titles_ar,
        "job_family": families,
        "min_grade": min_grade,
        "max_grade": max_grade,
        "min_education": min_education,
        "required_certifications": required,
        "safety_critical": safety,
    }
