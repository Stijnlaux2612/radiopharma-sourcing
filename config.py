"""Sourcing configuration.

Two things live here and they do different jobs — keep them separate in your head:

1. SEARCH TERMS are the discovery net. The pullers query ClinicalTrials.gov and
   openFDA by these, NOT by company. Every term widens coverage across *all*
   sponsors at once, including companies you've never heard of. This is where new
   leads come from. Adding a term does far more for discovery than adding a name.

2. UNIVERSE is the watchlist. Matching a record to a name here is a high-confidence
   signal and gets weighted up downstream. A company OFF the list still surfaces if
   it hits a strong catalyst — it just doesn't get the boost. So this list should be
   curated and deliberate, not exhaustive. Quality over length.
"""

# How far back each run looks. Run weekly; 10 days gives overlap so nothing
# slips through if a run is missed.
LOOKBACK_DAYS = 10

# ---------------------------------------------------------------------------
# DISCOVERY NET — intervention / modality terms for ClinicalTrials.gov v2.
# Each term = one query per run. Broad imaging tracers (generic F-18 FDG, generic
# Ga-68) are deliberately excluded: they'd flood the net with routine imaging
# trials. Keep terms specific to therapeutic / theranostic radiopharma.
# ---------------------------------------------------------------------------
RADIOPHARMA_TERMS = [
    # isotopes — therapeutic
    "lutetium Lu 177",
    "actinium Ac 225",
    "lead Pb 212",
    "copper Cu 67",
    "copper Cu 64",
    "terbium Tb 161",
    "yttrium Y 90",
    "radium Ra 223",
    "iodine I 131 therapy",
    "astatine At 211",
    "bismuth Bi 213",
    "scandium Sc 47",
    "zirconium Zr 89",
    # modality / mechanism
    "radioligand therapy",
    "radiopharmaceutical",
    "theranostic",
    "peptide receptor radionuclide therapy",
    "targeted alpha therapy",
    "radioconjugate",
    "radioimmunotherapy",
    # targets (radionuclide-delivered)
    "PSMA radionuclide",
    "somatostatin receptor radionuclide",
    "FAP targeted radionuclide",
    "GRPR radionuclide",

    # -----------------------------------------------------------------------
    # DIAGNOSTICS. Named agents and target-specific PET, not bare modality.
    # Generic "PET"/"SPECT"/"F-18 FDG" stay out — they return routine hospital
    # imaging, not company programmes. These terms name commercial tracers or a
    # specific target, so they surface the diagnostics developers on the
    # watchlist (Blue Earth, MedTrace, Nuclidium, Nucligen) and their peers.
    # -----------------------------------------------------------------------
    "PSMA PET",
    "FAPI PET",
    "somatostatin receptor PET",
    "piflufolastat",        # Pylarify — Lantheus
    "flotufolastat",        # Posluma — Blue Earth
    "gozetotide",           # PSMA-11 — Locametz / Illuccix
    "fluciclovine",         # Axumin — Blue Earth
    "fluoroestradiol",      # Cerianna — ER imaging
    "PSMA-1007",
    "DCFPyL",
]

# Device-side terms for openFDA 510(k)/PMA — the imaging/dosimetry adjacency.
DEVICE_TERMS = [
    "dosimetry",
    "radionuclide",
    "SPECT",
    "PET radiopharmaceutical",
    "radioligand planning",
]

# ---------------------------------------------------------------------------
# WATCHLIST — curated. A match here boosts a record's rank. Grouped by segment
# for maintenance only; the code treats it as a flat list.
# Acquired names are kept as match targets because trials/approvals often stay
# filed under the subsidiary — but M&A/financing signal now lives at the parent.
# ---------------------------------------------------------------------------
UNIVERSE = [
    # -- Large pharma with real radioligand programs --
    "Novartis",
    "Eli Lilly",
    "AstraZeneca",
    "Bristol Myers Squibb",
    "Bayer",

    # -- Acquired, kept as match targets (parent in parentheses) --
    "Fusion Pharmaceuticals",   # AstraZeneca
    "RayzeBio",                 # Bristol Myers Squibb
    "POINT Biopharma",          # Eli Lilly
    "Mariana Oncology",         # Eli Lilly

    # -- Commercial / mid-cap radiopharma --
    "Telix Pharmaceuticals",
    "Lantheus",
    "Perspective Therapeutics",
    "Y-mAbs Therapeutics",

    # -- Earlier-stage therapeutics --
    "ARTBIO",
    "Abdera Therapeutics",
    "AdvanCell",
    "Radionetics Oncology",
    "Convergent Therapeutics",
    "Aktis Oncology",           # IPO'd Jan 2026, Lilly anchor
    "Ratio Therapeutics",
    "Radiopharm Theranostics",
    "Actinium Pharmaceuticals",
    "Precirix",
    "Clarity Pharmaceuticals",
    "Debiopharm",
    "Alpha-9 Theranostics",
    "PentixaPharm",
    "Theragnostics",
    "Noria Therapeutics",
    "QSAM Therapeutics",        # Sm-153 bone therapy; found via CTG 2026-07

    # -- Diagnostics developers --
    "Blue Earth Diagnostics",
    "MedTrace",
    "Nuclidium",
    "Nucligen",
    "GE HealthCare",            # DaTscan (I-123 ioflupane); drugsfda 2026-04
    "Aphelion",                 # sponsor of record for PYLARIFY TRUVU (ORIG,
                                # 2026-03). Relationship to Lantheus/Progenics
                                # unverified — confirm before relying on it.

    # -- Isotope supply / production --
    "Curium",
    "Isotopia",
    "PanTera",
    "Nusano",
    "ITM Isotope Technologies",
    "Eckert & Ziegler",
    "SHINE Technologies",
    "NorthStar Medical Radioisotopes",
    "Orano Med",                # Pb-212 supply + therapeutics
    "Thor Medical",             # alpha-emitter supply (Pb-212 / At-211)
    "TerraThera",
    "Ionetix",                  # cyclotron isotopes (Ac-225, Cu-64)
    "Niowave",                  # accelerator-produced isotopes
    "TerraPower Isotopes",      # Ac-225 from thorium stock
    "BWXT Medical",
    "Nordion",                  # Mo-99 / medical isotopes
    "IRE ELiT",                 # Institut National des Radioéléments (Belgium)
    "NTP Radioisotopes",        # South Africa

    # -- CDMO / CRO (radiopharma-specific) --
    # Sourced from the supply-chain landscape (B04/B05). Kept radiopharma-native;
    # generalist peptide/chelator houses (Bachem, Lonza, CordenPharma) deliberately
    # excluded — radiopharma is a rounding error in their revenue.
    "SpectronRx",
    "Nucleus RadioPharma",
    "AtomVie Global Radiopharma",
    "Seibersdorf Laboratories",     # neutral state-owned CDMO (AIT)
    "ROTOP Pharmaka",
    "Moltek",                       # low-confidence profile — verify
    "PharmaLogic",
    "Jubilant Radiopharma",         # aka Jubilant DraxImage
    "Cardinal Health",              # nuclear pharmacy network
    "ABX advanced biochemical compounds",
    "SOFIE Biosciences",            # PET CDMO / cyclotron network
    "PETNET Solutions",             # Siemens PET manufacturing network
    "RLS Radiopharmacies",          # acquired by Telix Jan 2025 — signal now at Telix
    # -- CRO / preclinical imaging --
    "ABX-CRO",                      # Dresden; distinct from ABX precursor house
    "Minerva Imaging",
    "Invicro",                      # Konica Minolta
    "Oncodesign Services",
    "Median Technologies",

    # -- Chelator / precursor input nodes (radiopharma-native only) --
    # Generalist peptide houses (Bachem, PolyPeptide, CordenPharma) excluded —
    # GLP-1-driven, radiopharma is a rounding error. These two are native.
    "Macrocyclics",                 # Orano Med-owned, French state-linked
    "CheMatech",                    # independent, ICMUB spin-off

    # -- China / APAC --
    # Sinotau was found live; the other three came off the shortlist that used
    # to sit here as a comment. Unlike every other name added this session,
    # these three have not yet been seen in a live sponsor field, so their real
    # API spelling is unconfirmed and no aliases are guessed for them. Add
    # aliases when an actual sponsor string turns up that should have matched.
    "Sinotau Pharmaceutical Group",  # found via CTG 2026-07
    "Full-Life Technologies",
    "Zonsen PepLib",
    "Primo Biotechnology",
]

DB_PATH = "catalysts.db"
USER_AGENT = "catalyst-sourcing/0.1 (research use)"
REQUEST_TIMEOUT = 30

# CMS publishes HCPCS on a QUARTERLY cycle, and radiopharma codes land a few
# times a year at most (the most recent as of build was 2024-07-01). A 10-day
# window is structurally empty for this source, so the CMS puller treats this as
# a floor on its lookback — it is not a bug when it returns nothing.
CMS_MIN_LOOKBACK_DAYS = 120
CMS_HCPCS_PAGE = ("https://www.cms.gov/medicare/coding-billing/"
                  "healthcare-common-procedure-system/quarterly-update")
