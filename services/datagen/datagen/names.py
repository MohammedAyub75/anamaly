"""Bilingual name pools for Saudi, GCC and expatriate employees.

Every name exists as an English/Arabic pair, because the UI renders both and
because `name_en_normalised` -- the C06 fuzzy-match key -- only means anything if
`name_en` carries the messy real-world variation that normalisation strips.
The transliteration table is what makes that variation realistic: Mohammed,
Muhammad and Mohamed are the same person's name spelled three ways, and a
near-duplicate detector that cannot cope with that is not worth building.
"""

from __future__ import annotations

import re
import unicodedata

# (english, arabic) pairs. Arabic given names carry the same gender as English.
ARAB_MALE_GIVEN: tuple[tuple[str, str], ...] = (
    ("Mohammed", "محمد"), ("Abdullah", "عبدالله"), ("Ahmed", "أحمد"),
    ("Ali", "علي"), ("Khalid", "خالد"), ("Fahad", "فهد"), ("Saud", "سعود"),
    ("Omar", "عمر"), ("Yousef", "يوسف"), ("Ibrahim", "إبراهيم"),
    ("Sultan", "سلطان"), ("Nasser", "ناصر"), ("Bandar", "بندر"),
    ("Turki", "تركي"), ("Majed", "ماجد"), ("Salem", "سالم"),
    ("Hassan", "حسن"), ("Hussein", "حسين"), ("Rayan", "ريان"),
    ("Ziyad", "زياد"), ("Faisal", "فيصل"), ("Talal", "طلال"),
    ("Mishaal", "مشعل"), ("Anas", "أنس"), ("Musaed", "مساعد"),
    ("Waleed", "وليد"), ("Rakan", "راكان"), ("Badr", "بدر"),
    ("Saleh", "صالح"), ("Adel", "عادل"),
)

ARAB_FEMALE_GIVEN: tuple[tuple[str, str], ...] = (
    ("Noura", "نورة"), ("Sara", "سارة"), ("Reem", "ريم"), ("Hessa", "حصة"),
    ("Lama", "لمى"), ("Mona", "منى"), ("Aisha", "عائشة"), ("Fatimah", "فاطمة"),
    ("Maha", "مها"), ("Latifa", "لطيفة"), ("Amal", "أمل"), ("Dana", "دانة"),
    ("Ghada", "غادة"), ("Jawaher", "جواهر"), ("Munira", "منيرة"),
    ("Shatha", "شذى"), ("Wafa", "وفاء"), ("Rana", "رنا"), ("Haya", "هيا"),
    ("Asma", "أسماء"),
)

SAUDI_FAMILY: tuple[tuple[str, str], ...] = (
    ("Al Qahtani", "القحطاني"), ("Al Ghamdi", "الغامدي"), ("Al Otaibi", "العتيبي"),
    ("Al Shehri", "الشهري"), ("Al Dossary", "الدوسري"), ("Al Harbi", "الحربي"),
    ("Al Zahrani", "الزهراني"), ("Al Malki", "المالكي"), ("Al Subaie", "السبيعي"),
    ("Al Mutairi", "المطيري"), ("Al Anazi", "العنزي"), ("Al Juhani", "الجهني"),
    ("Al Amri", "العمري"), ("Al Balawi", "البلوي"), ("Al Rashidi", "الرشيدي"),
    ("Al Shammari", "الشمري"), ("Al Yami", "اليامي"), ("Al Bishi", "البيشي"),
    ("Al Faifi", "الفيفي"), ("Al Sulami", "السلمي"), ("Al Hazmi", "الحازمي"),
    ("Al Qurashi", "القرشي"), ("Bin Saleh", "بن صالح"), ("Al Ruwaili", "الرويلي"),
    ("Al Khalidi", "الخالدي"),
)

GCC_FAMILY: tuple[tuple[str, str], ...] = (
    ("Al Kuwari", "الكواري"), ("Al Mannai", "المناعي"), ("Al Sabah", "الصباح"),
    ("Al Khalifa", "آل خليفة"), ("Al Busaidi", "البوسعيدي"), ("Al Marri", "المري"),
    ("Al Nuaimi", "النعيمي"), ("Al Hashimi", "الهاشمي"), ("Al Balushi", "البلوشي"),
    ("Al Jaber", "الجابر"),
)

# Non-Arab expatriate pools, by ISO alpha-3, with an Arabic rendering that HR
# systems in the Kingdom genuinely carry alongside the Latin spelling.
EXPAT_GIVEN: dict[str, tuple[tuple[str, str], ...]] = {
    "IND": (("Rajesh", "راجيش"), ("Suresh", "سوريش"), ("Anil", "أنيل"),
            ("Vijay", "فيجاي"), ("Priya", "بريا"), ("Deepak", "ديباك"),
            ("Sunil", "سونيل"), ("Meena", "مينا"), ("Arun", "أرون"),
            ("Kavita", "كافيتا")),
    "PAK": (("Imran", "عمران"), ("Bilal", "بلال"), ("Asif", "آصف"),
            ("Nadia", "نادية"), ("Tariq", "طارق"), ("Shahid", "شهيد"),
            ("Ayesha", "عائشة"), ("Kamran", "كامران")),
    "EGY": (("Mahmoud", "محمود"), ("Mostafa", "مصطفى"), ("Karim", "كريم"),
            ("Hoda", "هدى"), ("Sherif", "شريف"), ("Nermin", "نرمين")),
    "PHL": (("Jose", "خوسيه"), ("Maria", "ماريا"), ("Ramon", "رامون"),
            ("Grace", "غريس"), ("Rodel", "رودل"), ("Liza", "ليزا")),
    "BGD": (("Rahim", "رحيم"), ("Kamal", "كمال"), ("Shirin", "شيرين"),
            ("Jamal", "جمال")),
    "SDN": (("Osman", "عثمان"), ("Siddig", "صديق"), ("Amna", "آمنة")),
    "LBN": (("Georges", "جورج"), ("Rania", "رانيا"), ("Elie", "إيلي")),
    "JOR": (("Zaid", "زيد"), ("Lina", "لينا"), ("Murad", "مراد")),
    "GBR": (("James", "جيمس"), ("Emma", "إيما"), ("Oliver", "أوليفر")),
    "USA": (("Michael", "مايكل"), ("Jennifer", "جينيفر"), ("Robert", "روبرت")),
}

EXPAT_FAMILY: dict[str, tuple[tuple[str, str], ...]] = {
    "IND": (("Kumar", "كومار"), ("Sharma", "شارما"), ("Nair", "ناير"),
            ("Reddy", "ريدي"), ("Menon", "مينون"), ("Patel", "باتيل")),
    "PAK": (("Khan", "خان"), ("Malik", "مالك"), ("Chaudhry", "تشودري"),
            ("Butt", "بت")),
    "EGY": (("Ibrahim", "إبراهيم"), ("Fawzy", "فوزي"), ("Zaki", "زكي"),
            ("Mansour", "منصور")),
    "PHL": (("Santos", "سانتوس"), ("Reyes", "رييس"), ("Cruz", "كروز"),
            ("Bautista", "باوتيستا")),
    "BGD": (("Hossain", "حسين"), ("Islam", "إسلام"), ("Alam", "عالم")),
    "SDN": (("Elhassan", "الحسن"), ("Bashir", "بشير")),
    "LBN": (("Haddad", "حداد"), ("Khoury", "خوري")),
    "JOR": (("Odeh", "عودة"), ("Barakat", "بركات")),
    "GBR": (("Smith", "سميث"), ("Wright", "رايت"), ("Clarke", "كلارك")),
    "USA": (("Johnson", "جونسون"), ("Miller", "ميلر"), ("Davis", "ديفيس")),
}

# Spellings of the same name. Applied at the transliteration noise rate so that
# name_en carries variation that name_en_normalised does not resolve away --
# which is exactly the C06 problem the detector has to solve.
TRANSLITERATIONS: dict[str, tuple[str, ...]] = {
    "Mohammed": ("Muhammad", "Mohamed", "Mohammad"),
    "Ahmed": ("Ahmad", "Ahmet"),
    "Abdullah": ("Abdallah", "Abdulla"),
    "Yousef": ("Yusuf", "Youssef"),
    "Hussein": ("Husain", "Hussain"),
    "Fatimah": ("Fatima", "Fatema"),
    "Aisha": ("Ayesha", "Aysha"),
    "Khalid": ("Khaled",),
    "Osman": ("Uthman",),
    "Ibrahim": ("Ebrahim",),
    "Al Qahtani": ("Alqahtani", "Al-Qahtani"),
    "Al Ghamdi": ("Alghamdi", "Al-Ghamdi"),
    "Al Otaibi": ("Alotaibi", "Al-Otaibi"),
    "Al Harbi": ("Alharbi", "Al-Harbi"),
}

DEGREE_FIELDS: tuple[str, ...] = (
    "Petroleum Engineering", "Mechanical Engineering", "Chemical Engineering",
    "Electrical Engineering", "Geology", "Industrial Engineering",
    "Computer Science", "Accounting", "Finance", "Business Administration",
    "Human Resources", "Occupational Safety", "Nursing", "Medicine",
    "Supply Chain Management", "Civil Engineering",
)

INSTITUTIONS: tuple[str, ...] = (
    "King Fahd University of Petroleum and Minerals",
    "King Saud University", "King Abdulaziz University",
    "King Abdullah University of Science and Technology",
    "Imam Abdulrahman Bin Faisal University", "Qassim University",
    "Jazan University", "Prince Mohammad Bin Fahd University",
    "Cairo University", "University of Mumbai", "NED University",
    "University of the Philippines", "University of Manchester",
    "Texas A&M University",
)

LANGUAGES: tuple[str, ...] = (
    "Arabic", "English", "Urdu", "Hindi", "Malayalam", "Tagalog", "Bengali",
    "French", "Tamil",
)

_PUNCTUATION = re.compile(r"[^A-Z ]+")
_SPACES = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Upper-cased, punctuation-stripped, single-spaced -- the C06 blocking key.

    `name_en` deliberately carries casing and spacing noise; this column is what
    a fuzzy match is computed on, so it must be derived from the noisy value
    rather than from the clean one, or the noise would be invisible.
    """
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _SPACES.sub(" ", _PUNCTUATION.sub(" ", folded.upper())).strip()
