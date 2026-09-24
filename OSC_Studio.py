#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSC Studio — mini-app de traitement Siril 1.4 (sirilpy + PyQt6)
===============================================================

Petite application « tout-en-un » pour caméra couleur (OSC), pensée pour
débuter facilement — et pour s'enchaîner avec le plugin N.I.N.A. « Mode
Débutant » : on lui donne LE DOSSIER DE LA NUIT tel que N.I.N.A. l'a écrit,
elle se débrouille avec ce qu'elle y trouve.

  1. On choisit le TYPE DE CIBLE (pipeline) :
        - Couleur (large bande)
        - Nébuleuse (bande étroite, filtre dual-band -> composite HOO)
  2. On choisit le DOSSIER DE LA NUIT, par ex. `Astro/2026-09-23`, qui contient
     ce que N.I.N.A. a produit : LIGHT/, et selon la nuit DARK/, FLAT/, BIAS/,
     DARKFLAT/. (Les anciens noms lights/darks/flats/biases marchent aussi.)
  3. L'app lit l'en-tête FITS de chaque photo et affiche les CIBLES trouvées
     (N.I.N.A. range toutes les cibles d'une nuit dans le même LIGHT/ : c'est
     le mot-clé OBJECT qui les sépare). On coche, on lance.

Ce qui n'est pas là n'est PAS bloquant — l'app adapte la calibration :
  * pas de darks  -> darks de la BIBLIOTHÈQUE si un master correspond (même
                     pose, gain, offset, température), sinon sans darks ;
  * pas de flats  -> pas de correction du vignettage (le retrait du gradient
                     en rattrape une partie) ;
  * flats sans bias -> bias SYNTHÉTIQUE (niveau de noir calculé à partir de
                     l'OFFSET écrit par N.I.N.A.), pour les caméras connues.
Chaque master dark fabriqué est rangé dans la bibliothèque, à côté des nuits
(`Astro/_Bibliotheque_Darks/`) : des darks faits UNE fois servent ensuite à
toutes les nuits aux mêmes réglages.

Sorties, dans le dossier de la nuit, une série par cible :
  * <Cible>.fit            -> empilement linéaire brut (à retraiter à la main)
  * <Cible>_processed.tif  -> TIFF 16-bit traité
  * <Cible>_preview.png    -> aperçu
Le travail intermédiaire se fait dans `<nuit>/_OSC_Studio/<Cible>/` (liens vers
tes fichiers d'origine, jamais modifiés ni déplacés).

Architecture (volontairement extensible) :
  * le « moteur » (lecture des en-têtes, inventaire, plan, programme Siril) ne
    dépend ni de Qt ni de Siril : il se teste seul ;
  * un pipeline = un objet Pipeline ; une étape optionnelle = un objet Step.

Portabilité macOS / Windows : pathlib partout, chemins transmis à Siril en
notation POSIX entre guillemets, liens physiques (repli : copie).
"""

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re
import shutil
import sys

# Réglages mémorisés entre deux lancements. Dans le dossier personnel (et non
# dans le dépôt) pour ne pas le polluer ; chemin construit via pathlib.
SETTINGS_PATH = Path.home() / ".osc_studio_settings.json"

# Dossier de travail créé DANS la nuit, et bibliothèque de darks À CÔTÉ des nuits
DOSSIER_TRAVAIL = "_OSC_Studio"
DOSSIER_BIBLIOTHEQUE = "_Bibliotheque_Darks"


def nom_de_sortie(cible):
    """« M 81 » -> « M81 », « NGC 7000 » -> « NGC7000 » ; sinon sanitize_name."""
    compact = re.sub(r"^([A-Za-z]{1,5})\s+(\d)", r"\1\2", (cible or "").strip())
    return sanitize_name(compact, "Cible")


def sanitize_name(raw, default="result"):
    """Nettoie un nom de sortie pour en faire un nom de fichier sûr et sans espace.

    Sans espace = pas besoin de guillemets, et le découpage des arguments Siril
    reste fiable. Retourne `default` si le résultat est vide.
    """
    raw = (raw or "").strip()
    if not raw:
        return default
    cleaned = re.sub(r"\s+", "_", raw)              # espaces -> underscore
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", cleaned)  # ne garder que sûr
    cleaned = re.sub(r"_+", "_", cleaned).strip("_.")
    # Exiger au moins un caractère alphanumérique : évite ".."/"." (remontée
    # d'arborescence) et les noms vides après nettoyage.
    if not re.search(r"[A-Za-z0-9]", cleaned):
        return default
    return cleaned


def chemin_siril(p: Path) -> str:
    """Chemin absolu pour une commande Siril : POSIX et entre guillemets
    (sirilpy recolle les arguments avec des espaces)."""
    return '"' + Path(p).resolve().as_posix() + '"'


# =============================================================================
#  LECTURE DES EN-TÊTES FITS (sans astropy : Siril ne l'embarque pas)
# =============================================================================

FITS_EXTS = {".fit", ".fits", ".fts"}

# Extensions comptées comme des images (brutes RAW ou FITS) dans les sous-dossiers.
# Le Canon R8 produit du .CR3 ; N.I.N.A. sauve du .fits.
IMAGE_EXTS = FITS_EXTS | {
    ".cr3", ".cr2", ".nef", ".arw", ".dng",     # RAW (Canon R8 = CR3)
    ".raw", ".pef", ".orf", ".raf",             # autres RAW
    ".tif", ".tiff", ".xisf", ".ser",
}


def _valeur_fits(texte):
    """Valeur d'une carte FITS : chaîne, booléen, entier ou réel."""
    v = texte.strip()
    if v.startswith("'"):
        sortie, i, s = "", 0, v[1:]
        while i < len(s):
            if s[i] == "'":
                if i + 1 < len(s) and s[i + 1] == "'":   # '' = apostrophe
                    sortie += "'"
                    i += 2
                    continue
                break
            sortie += s[i]
            i += 1
        return sortie.rstrip()
    v = v.split("/")[0].strip()
    if v == "T":
        return True
    if v == "F":
        return False
    for conv in (int, float):
        try:
            return conv(v)
        except ValueError:
            pass
    return v


def lire_entete(chemin: Path) -> dict:
    """En-tête principal d'un FITS ({} si illisible ou si ce n'est pas un FITS)."""
    if Path(chemin).suffix.lower() not in FITS_EXTS:
        return {}
    entete = {}
    try:
        with open(chemin, "rb") as f:
            for _ in range(200):                     # 200 blocs = garde-fou
                bloc = f.read(2880)
                if len(bloc) < 2880:
                    break
                for k in range(36):
                    carte = bloc[k * 80:(k + 1) * 80].decode("ascii", "replace")
                    cle = carte[:8].strip()
                    if cle == "END":
                        return entete
                    if carte[8:10] == "= ":
                        entete[cle] = _valeur_fits(carte[10:])
    except OSError:
        pass
    return entete


def _nombre(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


@dataclass
class Photo:
    chemin: Path
    entete: dict = field(default_factory=dict)

    @property
    def pose(self):
        return _nombre(self.entete.get("EXPTIME", self.entete.get("EXPOSURE")))

    @property
    def gain(self):
        return _nombre(self.entete.get("GAIN"))

    @property
    def offset(self):
        return _nombre(self.entete.get("OFFSET"))

    @property
    def binning(self):
        return _nombre(self.entete.get("XBINNING")) or 1

    @property
    def consigne(self):
        return _nombre(self.entete.get("SET-TEMP"))

    @property
    def temperature(self):
        return _nombre(self.entete.get("CCD-TEMP"))

    @property
    def cible(self):
        # N.I.N.A. écrit OBJECT. Le plugin Mode Débutant y mettait « M 81 — Bode's
        # Galaxy », dont le tiret devient « ? » dans le FITS : on garde le nom
        # de catalogue, avant le séparateur.
        nom = str(self.entete.get("OBJECT") or "")
        nom = re.split(r"\s+[?—–-]\s+", nom)[0]
        return re.sub(r"\s+", " ", nom).strip()

    def hors_temperature(self, consigne):
        """Photo prise à plus de 1 °C de la consigne FINALE de sa série.

        ⚠ Pas SET-TEMP de la photo elle-même : pendant la descente, N.I.N.A.
        écrit la consigne INTERMÉDIAIRE de sa rampe (vu le 23 sept 2026 :
        SET-TEMP -7 pour un capteur à -2,7 °C), si bien qu'une photo prise en
        plein refroidissement paraît « à température »."""
        t = self.temperature
        return consigne is not None and t is not None and abs(consigne - t) > 1.0


# =============================================================================
#  INVENTAIRE DU DOSSIER DE LA NUIT
# =============================================================================

# Noms de sous-dossiers reconnus (casse ignorée). Les premiers sont ceux de
# N.I.N.A. ($$IMAGETYPE$$), les suivants ceux des scripts Siril d'origine.
TYPES_DOSSIERS = {
    "lights": ("light", "lights"),
    "darks": ("dark", "darks"),
    "flats": ("flat", "flats"),
    "biases": ("bias", "biases", "offset", "offsets"),
    "darkflats": ("darkflat", "darkflats", "flatdark", "flatdarks", "dark_flat", "flat_dark"),
}

# Caméras dont le niveau de noir est connu : bias = facteur × OFFSET (en ADU
# 16 bits). SV405CC : 1920 ADU mesurés sur des darks à OFFSET 30 (23 sept 2026)
# = 64 × 30. Une caméra absente de cette table ne reçoit PAS de bias
# synthétique : mieux vaut pas de flats qu'un flat mal calibré.
FACTEUR_BIAS_OFFSET = {
    "SV405CC": 64,
}

# Capteur SPCC (base de données Siril) selon la caméra écrite par N.I.N.A.
# (INSTRUME). Inconnue -> `spcc` utilise les réglages de l'outil SPCC.
CAPTEURS_SPCC = {
    "SV405CC": "Sony IMX294",
    "ASI294MC": "Sony IMX294",
    "SV605CC": "Sony IMX533",
    "ASI533MC": "Sony IMX533",
    "ASI2600MC": "Sony IMX571",
    "ASI585MC": "Sony IMX585",
    "ASI662MC": "Sony IMX662",
    "ASI678MC": "Sony IMX678",
    "ASI183MC": "Sony IMX183",
    "ASI071MC": "Sony IMX071",
}


def _cherche_table(table, instrument):
    cle = re.sub(r"[^A-Z0-9]", "", str(instrument or "").upper())
    for modele, valeur in table.items():
        if modele in cle:
            return valeur
    return None


def trouver_sous_dossier(nuit: Path, genre: str):
    """Le sous-dossier d'un genre (lights, darks…), quelle que soit sa casse."""
    try:
        for enfant in nuit.iterdir():
            if enfant.is_dir() and enfant.name.lower() in TYPES_DOSSIERS[genre]:
                return enfant
    except OSError:
        pass
    return None


def lister_images(dossier) -> list:
    """Fichiers image directement dans le dossier (non récursif, comme `convert`)."""
    if dossier is None:
        return []
    try:
        return sorted(f for f in dossier.iterdir()
                      if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
    except OSError:
        return []


@dataclass
class Inventaire:
    nuit: Path
    dossiers: dict      # genre -> Path | None
    photos: dict        # genre -> [Photo]


def inventorier(nuit: Path) -> Inventaire:
    dossiers, photos = {}, {}
    for genre in TYPES_DOSSIERS:
        d = trouver_sous_dossier(nuit, genre)
        dossiers[genre] = d
        photos[genre] = [Photo(f, lire_entete(f)) for f in lister_images(d)]
    return Inventaire(nuit, dossiers, photos)


@dataclass
class Groupe:
    """Une série homogène : même cible, même pose, même gain/offset/binning."""
    cible: str
    pose: object
    gain: object
    offset: object
    binning: object
    photos: list = field(default_factory=list)     # retenues
    ecartees: list = field(default_factory=list)   # hors température
    consigne: object = None                        # consigne finale (la plus fréquente)

    @property
    def reference(self) -> Photo:
        return self.photos[0] if self.photos else self.ecartees[0]

    def resume(self) -> str:
        morceaux = [self.cible or "Cible sans nom"]
        if self.pose is not None:
            morceaux.append(f"{self.pose:g} s")
        if self.gain is not None:
            morceaux.append(f"gain {self.gain}")
        if self.consigne is not None:
            morceaux.append(f"{self.consigne:g} °C")
        texte = " · ".join(morceaux) + f" — {len(self.photos)} photos"
        if self.ecartees:
            texte += f" ({len(self.ecartees)} écartées : pas encore à température)"
        return texte


def consigne_finale(photos: list):
    """La consigne de température la plus fréquente d'une série (la rampe de
    refroidissement ne dure que quelques photos)."""
    compte = {}
    for p in photos:
        if p.consigne is not None:
            compte[round(p.consigne)] = compte.get(round(p.consigne), 0) + 1
    return max(compte, key=compte.get) if compte else None


def grouper(lights: list, ecarter_hors_temperature: bool, nom_defaut: str) -> list:
    """Sépare les photos d'une nuit en séries homogènes."""
    groupes = {}
    for p in lights:
        cle = (p.cible or nom_defaut, p.pose, p.gain, p.offset, p.binning)
        if cle not in groupes:
            groupes[cle] = Groupe(*cle)
        groupes[cle].photos.append(p)
    resultat = []
    for g in groupes.values():
        g.consigne = consigne_finale(g.photos)
        if ecarter_hors_temperature:
            toutes = g.photos
            g.photos = [p for p in toutes if not p.hors_temperature(g.consigne)]
            g.ecartees = [p for p in toutes if p.hors_temperature(g.consigne)]
        if not g.photos:
            # Aucune à température (refroidissement jamais atteint) : on garde
            # tout plutôt que de ne rien traiter.
            g.photos, g.ecartees = g.ecartees, []
        resultat.append(g)
    return sorted(resultat, key=lambda g: -len(g.photos))


def _egal(a, b, tolerance=0.0):
    """Égal, ou inconnu d'un côté (on ne rejette pas sur une info absente)."""
    if a is None or b is None:
        return True
    return abs(a - b) <= tolerance


def darks_assortis(groupe: Groupe, darks: list) -> list:
    """Les darks faits aux mêmes réglages que la série (et à température)."""
    return [d for d in darks
            if _egal(d.pose, groupe.pose, 0.5)
            and _egal(d.gain, groupe.gain)
            and _egal(d.offset, groupe.offset)
            and _egal(d.binning, groupe.binning)
            and _egal(d.temperature, groupe.consigne, 1.0)]


def nom_master_dark(groupe: Groupe) -> str:
    morceaux = ["MasterDark"]
    if groupe.pose is not None:
        morceaux.append(f"{groupe.pose:g}s")
    if groupe.gain is not None:
        morceaux.append(f"g{groupe.gain}")
    if groupe.offset is not None:
        morceaux.append(f"o{groupe.offset}")
    if groupe.binning and groupe.binning != 1:
        morceaux.append(f"bin{groupe.binning}")
    if groupe.consigne is not None:
        morceaux.append(f"{groupe.consigne:g}C")
    return "_".join(morceaux) + ".fit"


def bibliotheque(nuit: Path) -> Path:
    return nuit.parent / DOSSIER_BIBLIOTHEQUE


# =============================================================================
#  PLAN DE TRAITEMENT D'UNE SÉRIE
# =============================================================================

@dataclass
class Plan:
    groupe: Groupe
    nuit: Path
    nom: str                    # nom des fichiers de sortie
    darks: list                 # darks de la nuit, assortis
    dark_biblio: object         # Path d'un master de la bibliothèque, ou None
    flats: list
    biais_flats: list           # bias (ou dark-flats) pour calibrer les flats
    biais_synthetique: object   # "=64*$OFFSET" ou None
    capteur_spcc: object        # "Sony IMX294" ou None
    notes: list = field(default_factory=list)   # explications pour l'utilisateur

    @property
    def travail(self) -> Path:
        return self.nuit / DOSSIER_TRAVAIL / self.nom

    @property
    def avec_dark(self):
        return bool(self.darks) or self.dark_biblio is not None

    @property
    def avec_flat(self):
        return bool(self.flats) and (bool(self.biais_flats) or self.biais_synthetique is not None)


def preparer_plan(inv: Inventaire, groupe: Groupe, noms_pris: set) -> Plan:
    ref = groupe.reference
    instrument = ref.entete.get("INSTRUME", "")

    nom = nom_de_sortie(groupe.cible)
    if nom in noms_pris:                     # même cible, autre pose / autre gain
        nom = sanitize_name(f"{nom}_{groupe.pose:g}s" if groupe.pose is not None else nom + "_2")
    while nom in noms_pris:
        nom += "_bis"
    noms_pris.add(nom)

    notes = []

    # -- Darks : ceux de la nuit, sinon la bibliothèque, sinon rien --
    darks = darks_assortis(groupe, inv.photos["darks"])
    dark_biblio = None
    if darks:
        notes.append(f"Darks : {len(darks)} de cette nuit (même pose, gain et température) ✓")
    else:
        candidat = bibliotheque(inv.nuit) / nom_master_dark(groupe)
        if candidat.is_file():
            dark_biblio = candidat
            notes.append(f"Darks : master de la bibliothèque ✓ ({candidat.name})")
        elif inv.photos["darks"]:
            notes.append("Darks : ceux de cette nuit ne correspondent pas (pose, gain ou "
                         "température différents) → sans darks")
        else:
            notes.append("Darks : aucun → bruit thermique et pixels chauds non corrigés "
                         "(l'empilement en rejette une partie)")

    # -- Flats, et de quoi les calibrer --
    flats = [f for f in inv.photos["flats"] if _egal(f.binning, groupe.binning)]
    biais = inv.photos["biases"] or inv.photos["darkflats"]
    synthetique = None
    if flats and not biais:
        facteur = _cherche_table(FACTEUR_BIAS_OFFSET, instrument)
        if facteur is not None and ref.offset is not None:
            synthetique = f"={facteur}*$OFFSET"
            notes.append(f"Flats : {len(flats)} ✓ — bias synthétique ({facteur} × OFFSET)")
        else:
            notes.append(f"Flats : {len(flats)} trouvés mais IGNORÉS — il faut des BIAS ou "
                         "des DARKFLAT pour les calibrer avec cette caméra")
    elif flats:
        notes.append(f"Flats : {len(flats)} ✓ (calibrés par {len(biais)} bias/dark-flats)")
    else:
        notes.append("Flats : aucun → vignettage non corrigé (le retrait du gradient en "
                     "rattrape une partie)")

    plan = Plan(groupe, inv.nuit, nom, darks, dark_biblio, flats, biais, synthetique,
                _cherche_table(CAPTEURS_SPCC, instrument), notes)
    if not plan.avec_flat:
        plan.flats = []
    return plan


def materialiser(plan: Plan):
    """Prépare `<nuit>/_OSC_Studio/<nom>/` : un sous-dossier par genre, peuplé de
    LIENS vers les fichiers d'origine (repli : copie). Les originaux ne sont
    jamais touchés ; ce dossier-ci est à nous et repart de zéro à chaque fois."""
    racine = (plan.nuit / DOSSIER_TRAVAIL).resolve()
    travail = plan.travail.resolve()
    if racine not in travail.parents:        # garde-fou avant tout effacement
        raise RuntimeError(f"Dossier de travail inattendu : {travail}")
    if travail.exists():
        shutil.rmtree(travail)
    for sous, photos in (("lights", plan.groupe.photos), ("darks", plan.darks),
                         ("flats", plan.flats), ("biases", plan.biais_flats if plan.flats else [])):
        if not photos:
            continue
        cible = travail / sous
        cible.mkdir(parents=True, exist_ok=True)
        for p in photos:
            dest = cible / p.chemin.name
            try:
                os.link(p.chemin, dest)
            except OSError:
                shutil.copy2(p.chemin, dest)


# =============================================================================
#  PIPELINES ET PROGRAMME SIRIL
# =============================================================================

@dataclass
class Step:
    """Étape post-stack activable/désactivable (une case à cocher)."""
    key: str
    label: str
    enabled: bool = True


@dataclass
class Pipeline:
    """Un pipeline complet sélectionnable dans le menu déroulant."""
    key: str
    label: str
    description: str
    post_steps: list


PIPELINES = [
    Pipeline(
        "color",
        "Couleur — cible large bande",
        "Cible couleur classique (galaxies, amas, nébuleuses en RVB). "
        "Débayerise, empile, puis calibre les couleurs (SPCC) avant d'étirer.",
        [
            Step("gradient", "Retrait du gradient et de la pollution lumineuse  (subsky RBF)"),
            Step("color", "Calibration couleur photométrique  (platesolve + spcc)"),
            Step("green", "Suppression de la dominante verte  (rmgreen)"),
            Step("stretch", "Étirement automatique -> non linéaire  (autostretch -linked)"),
        ],
    ),
    Pipeline(
        "nebula",
        "Nébuleuse — bande étroite dual-band (HOO)",
        "Cible en bande étroite avec filtre dual-band. Extrait les couches Ha et "
        "OIII, les empile séparément, compose une image HOO (R=Ha, G=OIII, B=OIII) "
        "puis l'étire.",
        [
            Step("gradient", "Retrait du gradient / fond de ciel  (subsky)"),
            # Sur du HOO, nettoie les étoiles vertes mais décale l'OIII vers le bleu.
            Step("green", "Suppression de la dominante verte  (rmgreen)"),
            Step("stretch", "Étirement automatique -> non linéaire  (autostretch -linked)"),
        ],
    ),
]


@dataclass
class Etape:
    """Un bloc de commandes Siril. Fatale = son échec arrête la série ; sinon
    on note l'échec et on passe au bloc suivant (ex. SPCC hors ligne)."""
    phase: str
    commandes: list      # [[commande, arg, …], …]
    fatale: bool = True


def _coordonnees_cible(ref: Photo):
    """Centre visé, écrit par N.I.N.A. (OBJCTRA/OBJCTDEC), au format Siril.

    Surtout PAS RA/DEC : c'est la position que CROIT la monture, fausse de
    plusieurs degrés quand elle n'est pas synchronisée — alors qu'après le
    centrage de N.I.N.A. la cible est au centre à quelques secondes près."""
    ra = str(ref.entete.get("OBJCTRA") or "").strip()
    dec = str(ref.entete.get("OBJCTDEC") or "").strip()
    if not ra or not dec:
        return None
    return ra.replace(" ", ":") + "," + dec.replace(" ", ":")


def programme(plan: Plan, pipeline: Pipeline, etapes_actives: dict) -> list:
    """La suite de commandes Siril pour une série (exécutée dans `plan.travail`)."""
    E = []
    E.append(Etape("Préparation", [["cd", chemin_siril(plan.travail)]]))

    biais_flats = None
    if plan.flats:
        if plan.biais_flats:
            E.append(Etape("Master bias", [
                ["cd", "biases"], ["convert", "bias", "-out=../process"], ["cd", "../process"],
                ["stack", "bias", "rej", "3", "3", "-nonorm", "-out=../masters/bias_stacked"],
                ["cd", ".."]]))
            biais_flats = "-bias=../masters/bias_stacked"
        else:
            biais_flats = f'-bias="{plan.biais_synthetique}"'
        E.append(Etape("Master flat", [
            ["cd", "flats"], ["convert", "flat", "-out=../process"], ["cd", "../process"],
            ["calibrate", "flat", biais_flats],
            ["stack", "pp_flat", "rej", "3", "3", "-norm=mul", "-out=../masters/pp_flat_stacked"],
            ["cd", ".."]]))

    dark = None
    if plan.darks:
        E.append(Etape("Master dark", [
            ["cd", "darks"], ["convert", "dark", "-out=../process"], ["cd", "../process"],
            ["stack", "dark", "rej", "3", "3", "-nonorm", "-out=../masters/dark_stacked"],
            ["cd", ".."]]))
        dark = "-dark=../masters/dark_stacked"
    elif plan.dark_biblio is not None:
        dark = '"-dark=' + Path(plan.dark_biblio).resolve().as_posix() + '"'

    couleur = pipeline.key == "color"
    options = []
    if dark:
        options += [dark, "-cc=dark", "-cfa"]
    if plan.flats:
        options += ["-flat=../masters/pp_flat_stacked", "-equalize_cfa"]
        if not dark:
            # Sans dark, c'est le bias qui retire le niveau de noir des photos
            options.append(biais_flats)
    if couleur:
        options.append("-debayer")

    lights = [["cd", "lights"], ["convert", "light", "-out=../process"], ["cd", "../process"]]
    sequence = "light"
    if options:
        lights.append(["calibrate", "light", *options])
        sequence = "pp_light"
    E.append(Etape("Photos : conversion et calibration", lights))

    sortie = plan.nuit / plan.nom
    if couleur:
        E.append(Etape("Alignement et empilement", [
            ["register", sequence],
            ["stack", "r_" + sequence, "rej", "3", "3", "-norm=addscale", "-output_norm",
             "-rgb_equal", "-32b", "-out=result"],
            ["load", "result"], ["mirrorx", "-bottomup"],
            ["save", chemin_siril(sortie)]]))
    else:
        E.append(Etape("Extraction Ha / OIII, alignement et empilement", [
            ["seqextract_HaOIII", sequence, "-resample=ha"],
            ["register", "Ha_" + sequence],
            ["stack", "r_Ha_" + sequence, "rej", "3", "3", "-norm=addscale", "-output_norm",
             "-32b", "-out=results_00001"],
            ["mirrorx_single", "results_00001"],
            ["register", "OIII_" + sequence],
            ["stack", "r_OIII_" + sequence, "rej", "3", "3", "-norm=addscale", "-output_norm",
             "-32b", "-out=results_00002"],
            ["mirrorx_single", "results_00002"],
            ["register", "results", "-transf=shift", "-interp=none"],
            # Renormalisation OIII sur les statistiques de Ha (PixelMath)
            ["pm", "$r_results_00002$*mad($r_results_00001$)/mad($r_results_00002$)"
                   "-mad($r_results_00001$)/mad($r_results_00002$)*median($r_results_00002$)"
                   "+median($r_results_00001$)"],
            ["save", "OIII_renorm"],
            ["rgbcomp", "r_results_00001", "OIII_renorm", "OIII_renorm", "-out=result"],
            ["load", "result"],
            ["save", chemin_siril(sortie)]]))

    # -- Étapes optionnelles, dans l'ordre (SPCC AVANT l'étirement) --
    if etapes_actives.get("gradient"):
        # Couleur : RBF, qui suit les gradients tordus d'un ciel de ville (testé
        # le 23 sept 2026 sur M81 : bien meilleur que le plan de degré 1).
        # Nébuleuse : plan de degré 1, qui ne risque pas de manger une grande
        # nébulosité diffuse.
        commande = (["subsky", "-rbf", "-samples=20", "-tolerance=1.0", "-smooth=0.5"]
                    if couleur else ["subsky", "1"])
        E.append(Etape("Retrait du gradient", [commande], fatale=False))
    if couleur and etapes_actives.get("color"):
        ref = plan.groupe.reference
        resolution = ["platesolve"]
        centre = _coordonnees_cible(ref)
        if centre:
            resolution.append(centre)
        focale, pixel = _nombre(ref.entete.get("FOCALLEN")), _nombre(ref.entete.get("XPIXSZ"))
        if focale:
            resolution.append(f"-focal={focale:g}")
        if pixel:
            resolution.append(f"-pixelsize={pixel:g}")
        spcc = ["spcc"]
        if plan.capteur_spcc:
            # Siril veut les guillemets AUTOUR de tout l'argument (espace dans le nom)
            spcc.append(f'"-oscsensor={plan.capteur_spcc}"')
        # Non fatale : sans Internet ou sans catalogue, l'image sort quand même
        # (couleurs non calibrées) au lieu de tout perdre.
        E.append(Etape("Calibration couleur (platesolve + SPCC)", [resolution, spcc], fatale=False))
    if etapes_actives.get("green"):
        E.append(Etape("Suppression du vert", [["rmgreen"]], fatale=False))
    if etapes_actives.get("stretch"):
        E.append(Etape("Étirement", [["autostretch", "-linked"]], fatale=False))

    E.append(Etape("Sauvegarde du TIFF et de l'aperçu", [
        ["savetif", chemin_siril(plan.nuit / f"{plan.nom}_processed")],
        # Aperçu en JPG : le PNG pleine taille pesait 55 Mo
        ["savejpg", chemin_siril(plan.nuit / f"{plan.nom}_preview"), "85"],
        ["cd", chemin_siril(plan.nuit)],
        ["close"]]))
    return E


def programme_en_ssf(etapes: list) -> str:
    """Le même programme sous forme de script .ssf (tests, ou pour l'archiver)."""
    lignes = ["requires 1.4.0"]
    for e in etapes:
        lignes.append(f"# --- {e.phase}{'' if e.fatale else '  (non fatale)'}")
        lignes += [" ".join(c) for c in e.commandes]
    return "\n".join(lignes) + "\n"


def apres_succes(plan: Plan, nettoyer: bool, journal):
    """Travail de fichiers après une série réussie."""
    # Le master dark de cette nuit rejoint la bibliothèque (s'il n'y est pas)
    if plan.darks:
        masters = plan.travail / "masters"
        for ext in (".fit", ".fits", ".fts"):
            source = masters / ("dark_stacked" + ext)
            if source.is_file():
                dest = bibliotheque(plan.nuit) / nom_master_dark(plan.groupe)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        shutil.copy2(source, dest)
                        journal(f"Master dark rangé dans la bibliothèque : {dest.name}")
                except OSError as e:
                    journal(f"⚠ Master dark non rangé dans la bibliothèque : {e}")
                break
    # Les fichiers intermédiaires (plusieurs Go) ne servent plus
    if nettoyer:
        shutil.rmtree(plan.travail / "process", ignore_errors=True)


# =============================================================================
#  INTERFACE (Siril + Qt) — rien au-dessus de cette ligne n'en dépend
# =============================================================================

import sirilpy as s
from sirilpy import SirilConnectionError

# Installe PyQt6 dans le venv du script si nécessaire (protégé : si on est
# hors-ligne mais que PyQt6 est déjà là, l'import ci-dessous fonctionne quand même).
try:
    s.ensure_installed("PyQt6")
except Exception:
    pass

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QGroupBox, QComboBox, QTextEdit, QFileDialog,
    QMessageBox, QDialog, QTextBrowser, QProgressBar, QScrollArea, QFrame,
    QListWidget, QListWidgetItem,
)
from PyQt6.QtCore import QUrl, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QDesktopServices, QPixmap


class ProcessingWorker(QThread):
    """Exécute les séries dans un thread séparé pour ne pas figer la fenêtre.

    Règle de sécurité (API sirilpy expérimentale) : SEUL ce thread touche la
    connexion Siril. Le thread principal ne fait que réagir aux signaux ci-dessous
    pour mettre à jour l'affichage — il n'appelle jamais siril.* pendant un run.
    """

    sig_log = pyqtSignal(str)            # ligne à ajouter au journal
    sig_progress = pyqtSignal(int, int)  # (commandes faites, total)
    sig_phase = pyqtSignal(str)          # libellé de l'étape en cours
    sig_done = pyqtSignal(bool, str, list)   # (tout réussi ?, message, noms réussis)

    def __init__(self, siril, plans, pipeline, etapes_actives, nettoyer):
        super().__init__()
        self.siril = siril
        self.plans = plans
        self.pipeline = pipeline
        self.etapes_actives = etapes_actives
        self.nettoyer = nettoyer
        self._stop = False

    def request_stop(self):
        """Demande l'arrêt : il prend effet à la fin de la commande en cours."""
        self._stop = True

    def _emit_log(self, msg):
        self.sig_log.emit(msg)
        try:
            self.siril.log(msg)
        except Exception:
            pass

    def run(self):
        programmes = [(plan, programme(plan, self.pipeline, self.etapes_actives))
                      for plan in self.plans]
        total = sum(len(e.commandes) for _, prog in programmes for e in prog)
        fait = 0
        reussis, echecs = [], []
        self.sig_progress.emit(0, total)
        for plan, prog in programmes:
            tag = f"[{plan.nom}] " if len(programmes) > 1 else ""
            try:
                self.sig_phase.emit(tag + "Préparation des fichiers")
                materialiser(plan)
                for etape in prog:
                    for commande in etape.commandes:
                        if self._stop:
                            self.sig_done.emit(False, "Traitement arrêté par l'utilisateur.", reussis)
                            return
                        self.sig_phase.emit(tag + etape.phase)
                        self._emit_log("> " + " ".join(commande))
                        try:
                            self.siril.cmd(*commande)
                        except SirilConnectionError:
                            raise
                        except Exception as e:
                            if etape.fatale:
                                raise
                            self._emit_log(f"⚠ « {etape.phase} » sautée : {e}")
                            fait += len(etape.commandes) - etape.commandes.index(commande)
                            self.sig_progress.emit(fait, total)
                            break
                        fait += 1
                        self.sig_progress.emit(fait, total)
                apres_succes(plan, self.nettoyer, self._emit_log)
                reussis.append(plan.nom)
            except SirilConnectionError as e:
                self.sig_done.emit(False, f"Connexion perdue : {e}", reussis)
                return
            except Exception as e:
                self._emit_log(f"ERREUR sur {plan.nom} : {e}")
                echecs.append(f"{plan.nom} : {e}")
                try:
                    self.siril.cmd("cd", chemin_siril(plan.nuit))
                except Exception:
                    pass
        self.sig_done.emit(not echecs, "\n".join(echecs), reussis)


_NUIT_DATEE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def derniere_nuit(racine: Path):
    """Le dossier daté le plus récent (AAAA-MM-JJ) qui contient des photos."""
    try:
        nuits = sorted((d for d in racine.iterdir()
                        if d.is_dir() and _NUIT_DATEE.match(d.name)
                        and trouver_sous_dossier(d, "lights") is not None),
                       key=lambda d: d.name, reverse=True)
    except OSError:
        return None
    return nuits[0] if nuits else None


# Couleurs du thème (reprises dans la feuille de style et dans le code)
VERT, ORANGE, ROUGE, GRIS, ACCENT = "#3ba55d", "#d9822b", "#d83c3c", "#8a92a8", "#5b6cff"


class Tuile(QFrame):
    """Un compteur de la nuit : grand chiffre, libellé, état en couleur."""

    def __init__(self, icone, libelle):
        super().__init__()
        self.setObjectName("tuile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)
        self.chiffre = QLabel("—")
        self.chiffre.setObjectName("tuileChiffre")
        self.titre = QLabel(f"{icone}  {libelle}")
        self.titre.setObjectName("tuileTitre")
        self.etat = QLabel("")
        self.etat.setObjectName("tuileEtat")
        self.etat.setWordWrap(True)
        lay.addWidget(self.chiffre)
        lay.addWidget(self.titre)
        lay.addWidget(self.etat)

    def regler(self, n, etat, couleur):
        self.chiffre.setText(str(n) if n else "—")
        self.etat.setText(etat)
        self.setStyleSheet(f"QFrame#tuile {{ border-top: 3px solid {couleur}; }}"
                           f"QLabel#tuileEtat {{ color: {couleur}; }}")


class CarteCible(QFrame):
    """Une série de la nuit : case à cocher, résumé, badges de calibration."""

    def __init__(self, plan, cochee, quand_change):
        super().__init__()
        self.setObjectName("carteCible")
        self.plan = plan
        g = plan.groupe
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        haut = QHBoxLayout()
        self.case = QCheckBox(g.cible or plan.nom)
        self.case.setObjectName("carteTitre")
        self.case.setChecked(cochee)
        self.case.toggled.connect(quand_change)
        haut.addWidget(self.case, 1)
        details = [f"{len(g.photos)} photos"]
        if g.pose is not None:
            details.append(f"{g.pose:g} s")
            minutes = len(g.photos) * g.pose / 60
            details.append(f"{minutes / 60:.1f} h" if minutes >= 60 else f"{minutes:.0f} min au total")
        if g.gain is not None:
            details.append(f"gain {g.gain}")
        if g.consigne is not None:
            details.append(f"{g.consigne:g} °C")
        info = QLabel(" · ".join(details))
        info.setObjectName("carteInfo")
        haut.addWidget(info)
        lay.addLayout(haut)

        badges = QHBoxLayout()
        badges.setSpacing(6)
        if plan.darks:
            badges.addWidget(self._badge("Darks de la nuit ✓", VERT))
        elif plan.dark_biblio is not None:
            badges.addWidget(self._badge("Darks bibliothèque ✓", VERT))
        else:
            badges.addWidget(self._badge("Sans darks", ORANGE))
        badges.addWidget(self._badge("Flats ✓", VERT) if plan.flats
                         else self._badge("Sans flats", ORANGE))
        if plan.capteur_spcc:
            badges.addWidget(self._badge(f"Couleurs : {plan.capteur_spcc}", GRIS))
        if g.ecartees:
            badges.addWidget(self._badge(f"{len(g.ecartees)} écartées (refroidissement)", GRIS))
        badges.addStretch(1)
        lay.addLayout(badges)

        sortie = QLabel(f"→ {plan.nom}.fit · {plan.nom}_processed.tif")
        sortie.setObjectName("carteSortie")
        lay.addWidget(sortie)

    @staticmethod
    def _badge(texte, couleur):
        b = QLabel(texte)
        b.setStyleSheet(f"color:{couleur}; border:1px solid {couleur}; border-radius:9px;"
                        "padding:1px 8px; font-size:9pt;")
        return b


class OscStudioWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setObjectName("window")
        self.setWindowTitle("OSC Studio — Siril")
        self.setMinimumSize(760, 820)

        self.siril = None
        self.connected = False
        self.checkboxes = []          # étapes du pipeline courant
        self.cartes = []              # une CarteCible par série de la nuit
        self._worker = None
        self._inventaire = None
        self._dernier_resultat = None  # (nuit, nom) de la dernière image réussie

        self._build_ui()
        self.setStyleSheet(self._stylesheet())
        self._connect_to_siril()
        self._rebuild_steps()
        self._apply_settings(self._load_settings())
        self._analyser_dossier()

    # =====================================================================
    #  Construction
    # =====================================================================
    def _carte(self, numero, titre):
        box = QFrame()
        box.setObjectName("carte")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(10)
        entete = QHBoxLayout()
        pastille = QLabel(str(numero))
        pastille.setObjectName("pastille")
        pastille.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pastille.setFixedSize(26, 26)
        entete.addWidget(pastille)
        t = QLabel(titre)
        t.setObjectName("carteTitreSection")
        entete.addWidget(t, 1)
        lay.addLayout(entete)
        return box, lay, entete

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------- En-tête -------------------------------------------
        header = QWidget()
        header.setObjectName("header")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(22, 14, 22, 14)
        tb = QVBoxLayout()
        tb.setSpacing(0)
        title = QLabel("🔭  OSC Studio")
        title.setObjectName("title")
        subtitle = QLabel("De la nuit N.I.N.A. à l'image finie — Siril 1.4")
        subtitle.setObjectName("subtitle")
        tb.addWidget(title)
        tb.addWidget(subtitle)
        hlay.addLayout(tb)
        hlay.addStretch(1)
        self.conn_lbl = QLabel("●  Connexion…")
        self.conn_lbl.setObjectName("conn")
        hlay.addWidget(self.conn_lbl)
        aide = QPushButton("Aide")
        aide.setObjectName("ghost")
        aide.clicked.connect(self.show_about)
        hlay.addWidget(aide)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("content")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)

        # ---------------- ① La nuit -----------------------------------------
        carte, lay, entete = self._carte(1, "La nuit")
        self.nuit_btn = QPushButton("📅  Dernière nuit")
        self.nuit_btn.setObjectName("ghost")
        self.nuit_btn.setToolTip("Le dossier daté le plus récent, à côté de celui-ci")
        self.nuit_btn.clicked.connect(self._derniere_nuit)
        entete.addWidget(self.nuit_btn)

        row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("Le dossier daté écrit par N.I.N.A. — ex. Astro/2026-09-23")
        self.dir_edit.editingFinished.connect(self._analyser_dossier)
        row.addWidget(self.dir_edit, 1)
        self.browse_btn = QPushButton("Parcourir…")
        self.browse_btn.setObjectName("ghost")
        self.browse_btn.clicked.connect(self._choose_dir)
        row.addWidget(self.browse_btn)
        lay.addLayout(row)

        tuiles = QHBoxLayout()
        tuiles.setSpacing(10)
        self.tuiles = {
            "lights": Tuile("📷", "Photos"),
            "darks": Tuile("🌡", "Darks"),
            "flats": Tuile("☀", "Flats"),
            "biases": Tuile("⚫", "Bias"),
        }
        for t in self.tuiles.values():
            tuiles.addWidget(t, 1)
        lay.addLayout(tuiles)
        self.biblio_lbl = QLabel("")
        self.biblio_lbl.setObjectName("sectionHint")
        self.biblio_lbl.setWordWrap(True)
        lay.addWidget(self.biblio_lbl)
        layout.addWidget(carte)

        # ---------------- ② Les cibles --------------------------------------
        carte, lay, _ = self._carte(2, "Les cibles de la nuit")
        self.cibles_hint = QLabel("")
        self.cibles_hint.setObjectName("sectionHint")
        self.cibles_hint.setWordWrap(True)
        lay.addWidget(self.cibles_hint)
        self.cibles_layout = QVBoxLayout()
        self.cibles_layout.setSpacing(8)
        lay.addLayout(self.cibles_layout)
        self.temp_check = QCheckBox("Écarter les photos prises pendant le refroidissement")
        self.temp_check.setChecked(True)
        self.temp_check.toggled.connect(self._analyser_dossier)
        lay.addWidget(self.temp_check)
        layout.addWidget(carte)

        # ---------------- ③ Le traitement -----------------------------------
        carte, lay, _ = self._carte(3, "Le traitement")
        self.pipeline_combo = QComboBox()
        for p in PIPELINES:
            self.pipeline_combo.addItem(p.label)
        self.pipeline_combo.currentIndexChanged.connect(self._rebuild_steps)
        lay.addWidget(self.pipeline_combo)
        self.pipe_desc = QLabel("")
        self.pipe_desc.setObjectName("sectionHint")
        self.pipe_desc.setWordWrap(True)
        lay.addWidget(self.pipe_desc)
        self.steps_layout = QVBoxLayout()
        self.steps_layout.setSpacing(2)
        lay.addLayout(self.steps_layout)
        self.clean_check = QCheckBox("Supprimer les fichiers intermédiaires à la fin (plusieurs Go)")
        self.clean_check.setChecked(True)
        lay.addWidget(self.clean_check)
        layout.addWidget(carte)

        # ---------------- Lancer --------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.run_btn = QPushButton("🚀   Lancer le traitement")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.on_run)
        btn_row.addWidget(self.run_btn, 1)
        self.stop_btn = QPushButton("Arrêter")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self.on_stop)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.stop_btn)
        layout.addLayout(btn_row)

        self.phase_lbl = QLabel("")
        self.phase_lbl.setObjectName("phase")
        self.phase_lbl.setWordWrap(True)
        layout.addWidget(self.phase_lbl)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        layout.addWidget(self.progress)

        # ---------------- Résultat (caché tant qu'il n'y en a pas) ----------
        self.resultat = QFrame()
        self.resultat.setObjectName("carte")
        rlay = QVBoxLayout(self.resultat)
        rlay.setContentsMargins(16, 14, 16, 16)
        rlay.setSpacing(10)
        self.resultat_titre = QLabel("✨  Résultat")
        self.resultat_titre.setObjectName("carteTitreSection")
        rlay.addWidget(self.resultat_titre)
        self.preview_lbl = QLabel()
        self.preview_lbl.setObjectName("preview")
        self.preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rlay.addWidget(self.preview_lbl)
        rbtn = QHBoxLayout()
        self.ouvrir_image_btn = QPushButton("Ouvrir l'image")
        self.ouvrir_image_btn.clicked.connect(self._ouvrir_image)
        rbtn.addWidget(self.ouvrir_image_btn)
        self.ouvrir_dossier_btn = QPushButton("Ouvrir le dossier")
        self.ouvrir_dossier_btn.setObjectName("ghost")
        self.ouvrir_dossier_btn.clicked.connect(self._ouvrir_dossier)
        rbtn.addWidget(self.ouvrir_dossier_btn)
        rbtn.addStretch(1)
        rlay.addLayout(rbtn)
        self.resultat.setVisible(False)
        layout.addWidget(self.resultat)

        # ---------------- Journal (replié) ----------------------------------
        self.journal_btn = QPushButton("▸  Journal détaillé")
        self.journal_btn.setObjectName("lien")
        self.journal_btn.setCheckable(True)
        self.journal_btn.toggled.connect(self._basculer_journal)
        layout.addWidget(self.journal_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(160)
        self.log_view.setVisible(False)
        layout.addWidget(self.log_view)

        layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _basculer_journal(self, visible):
        self.log_view.setVisible(visible)
        self.journal_btn.setText(("▾" if visible else "▸") + "  Journal détaillé")

    # =====================================================================
    #  Thème
    # =====================================================================
    def _stylesheet(self):
        return f"""
        QWidget#window, QScrollArea#scroll, QWidget#content {{ background-color: #0f1117; }}
        QWidget {{ color: #dfe3ee; font-size: 10.5pt; }}

        QWidget#header {{ background-color: #14161f; border-bottom: 1px solid #242838; }}
        QLabel#title {{ font-size: 19pt; font-weight: 800; color: #f4f6fc; }}
        QLabel#subtitle {{ color: {GRIS}; font-size: 9.5pt; }}
        QLabel#conn {{ font-weight: 600; color: {GRIS}; }}
        QLabel#sectionHint {{ color: {GRIS}; }}
        QLabel#phase {{ color: #cfd5e6; font-weight: 600; }}

        QFrame#carte {{
            background-color: #171a24;
            border: 1px solid #262a38;
            border-radius: 12px;
        }}
        QLabel#pastille {{
            background-color: {ACCENT}; color: white; border-radius: 13px;
            font-weight: 800; font-size: 10pt;
        }}
        QLabel#carteTitreSection {{ font-size: 12.5pt; font-weight: 700; color: #f0f2f8; }}

        QFrame#tuile {{
            background-color: #10131b;
            border: 1px solid #262a38;
            border-radius: 10px;
        }}
        QLabel#tuileChiffre {{ font-size: 20pt; font-weight: 800; color: #f4f6fc; }}
        QLabel#tuileTitre {{ color: #aab2ca; font-weight: 600; }}
        QLabel#tuileEtat {{ font-size: 9pt; }}

        QFrame#carteCible {{
            background-color: #10131b;
            border: 1px solid #2a3145;
            border-radius: 10px;
        }}
        QCheckBox#carteTitre {{ font-size: 12pt; font-weight: 700; color: #f4f6fc; }}
        QLabel#carteInfo {{ color: #aab2ca; }}
        QLabel#carteSortie {{ color: #6a7185; font-family: Consolas, Menlo, monospace; font-size: 9pt; }}

        QLineEdit, QComboBox {{
            background-color: #0c0f16;
            border: 1px solid #2a3145;
            border-radius: 8px;
            padding: 8px 10px;
            color: #e6e9f2;
            selection-background-color: {ACCENT};
        }}
        QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
        QLineEdit:disabled {{ color: #6a7185; background-color: #12151d; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox QAbstractItemView {{
            background-color: #171a24; border: 1px solid #2a3145;
            selection-background-color: {ACCENT}; color: #e6e9f2; outline: none; padding: 4px;
        }}

        QCheckBox {{ spacing: 8px; padding: 3px 0; color: #dfe3ee; }}
        QCheckBox:disabled {{ color: #6a7185; }}

        QPushButton {{
            background-color: #232838; color: #e6e9f2;
            border: 1px solid #323a4f; border-radius: 8px; padding: 8px 14px;
        }}
        QPushButton:hover {{ background-color: #2b3145; border-color: #3e475f; }}
        QPushButton:pressed {{ background-color: #1d2231; }}
        QPushButton:disabled {{ color: #6a7185; background-color: #191d28; border-color: #252a38; }}
        QPushButton#primary {{
            background-color: {ACCENT}; border: none; color: #ffffff;
            font-weight: 700; font-size: 12pt; padding: 14px 18px; border-radius: 10px;
        }}
        QPushButton#primary:hover {{ background-color: #6f7dff; }}
        QPushButton#primary:pressed {{ background-color: #4a5ae6; }}
        QPushButton#primary:disabled {{ background-color: #2b3150; color: #8a90ad; }}
        QPushButton#danger {{ background-color: transparent; border: 1px solid #7a2f2f; color: #ff8a7a; }}
        QPushButton#danger:hover {{ background-color: #3a1f22; }}
        QPushButton#danger:disabled {{ color: #5a5560; border-color: #2a2230; }}
        QPushButton#ghost {{
            background-color: transparent; border: 1px solid #323a4f; color: #cfd5e6; padding: 6px 14px;
        }}
        QPushButton#ghost:hover {{ background-color: #20263a; border-color: #3e475f; }}
        QPushButton#lien {{ background: transparent; border: none; color: {GRIS}; padding: 2px 0; }}
        QPushButton#lien:hover {{ color: #cfd5e6; }}

        QProgressBar {{ background-color: #0c0f16; border: none; border-radius: 5px; }}
        QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 5px; }}

        QLabel#preview {{ border-radius: 8px; background-color: #0b0e14; padding: 6px; }}
        QTextEdit#logView {{
            background-color: #0b0e14; border: 1px solid #232a3b; border-radius: 8px;
            color: #b9c0d4; font-family: Consolas, Menlo, monospace; font-size: 9pt; padding: 6px;
        }}

        QDialog, QMessageBox {{ background-color: #12141c; }}
        QTextBrowser {{ background-color: #0b0e14; border: 1px solid #232a3b; border-radius: 8px; padding: 6px; }}

        QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: #2c3346; border-radius: 6px; min-height: 30px; }}
        QScrollBar::handle:vertical:hover {{ background: #3a445e; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """

    # =====================================================================
    #  Pipeline et réglages
    # =====================================================================
    def _current_pipeline(self):
        return PIPELINES[self.pipeline_combo.currentIndex()]

    def _rebuild_steps(self):
        self.pipe_desc.setText(self._current_pipeline().description)
        while self.steps_layout.count():
            item = self.steps_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.checkboxes = []
        for step in self._current_pipeline().post_steps:
            cb = QCheckBox(step.label)
            cb.setChecked(step.enabled)
            cb._step_key = step.key
            self.checkboxes.append(cb)
            self.steps_layout.addWidget(cb)

    def _load_settings(self):
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _apply_settings(self, data):
        try:
            idx = int(data.get("pipeline_index", 0))
            if 0 <= idx < self.pipeline_combo.count():
                self.pipeline_combo.setCurrentIndex(idx)
            steps = data.get("steps", {})
            for cb in self.checkboxes:
                if cb._step_key in steps:
                    cb.setChecked(bool(steps[cb._step_key]))
            self.temp_check.blockSignals(True)
            self.temp_check.setChecked(bool(data.get("ecarter_temperature", True)))
            self.temp_check.blockSignals(False)
            self.clean_check.setChecked(bool(data.get("nettoyer", True)))
            self.dir_edit.setText(data.get("directory", "") or "")
        except Exception:
            pass

    def _gather_settings(self):
        return {
            "directory": self.dir_edit.text(),
            "pipeline_index": self.pipeline_combo.currentIndex(),
            "steps": {cb._step_key: cb.isChecked() for cb in self.checkboxes},
            "ecarter_temperature": self.temp_check.isChecked(),
            "nettoyer": self.clean_check.isChecked(),
        }

    def _save_settings(self):
        try:
            SETTINGS_PATH.write_text(json.dumps(self._gather_settings(), indent=2), encoding="utf-8")
        except Exception:
            pass

    # =====================================================================
    #  La nuit
    # =====================================================================
    def _choose_dir(self):
        texte = self.dir_edit.text().strip()
        depart = str(Path(texte).parent) if texte else ""
        chosen = QFileDialog.getExistingDirectory(self, "Choisir le dossier de la nuit", depart)
        if chosen:
            self.dir_edit.setText(chosen)
            self._analyser_dossier()

    def _derniere_nuit(self):
        texte = self.dir_edit.text().strip()
        if not texte:
            self._choose_dir()
            return
        actuel = Path(texte)
        # Le dossier choisi est une nuit -> on cherche à côté ; sinon dedans
        racine = actuel.parent if _NUIT_DATEE.match(actuel.name) else actuel
        nuit = derniere_nuit(racine)
        if nuit is None:
            QMessageBox.information(self, "Aucune nuit",
                                    f"Aucun dossier daté (AAAA-MM-JJ) avec des photos dans :\n{racine}")
            return
        self.dir_edit.setText(str(nuit))
        self._analyser_dossier()

    def _vider_cartes(self):
        while self.cibles_layout.count():
            item = self.cibles_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.cartes = []

    def _analyser_dossier(self):
        """Lit la nuit : compteurs, séries, et ce que chaque série aura."""
        self._vider_cartes()
        self._inventaire = None
        texte = self.dir_edit.text().strip()
        nuit = Path(texte) if texte else None
        for t in self.tuiles.values():
            t.regler(0, "", GRIS)
        if nuit is None or not nuit.is_dir():
            self.biblio_lbl.setText("")
            self.cibles_hint.setText("Choisis d'abord le dossier de la nuit." if not texte
                                     else "✗ Dossier introuvable.")
            self._maj_bouton()
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            inv = inventorier(nuit)
        finally:
            QApplication.restoreOverrideCursor()
        self._inventaire = inv
        n = {g: len(v) for g, v in inv.photos.items()}

        self.tuiles["lights"].regler(n["lights"], "prêtes" if n["lights"] else "dossier LIGHT vide ou absent",
                                     VERT if n["lights"] else ROUGE)
        self.tuiles["darks"].regler(n["darks"], "de cette nuit" if n["darks"] else "bibliothèque ou sans",
                                    VERT if n["darks"] else ORANGE)
        self.tuiles["flats"].regler(n["flats"], "vignettage corrigé" if n["flats"] else "vignettage non corrigé",
                                    VERT if n["flats"] else ORANGE)
        nb_bias = n["biases"] + n["darkflats"]
        self.tuiles["biases"].regler(nb_bias, "pour les flats" if nb_bias
                                     else ("synthétique si possible" if n["flats"] else "pas nécessaire"),
                                     VERT if nb_bias else GRIS)
        biblio = bibliotheque(nuit)
        masters = sorted(biblio.glob("MasterDark_*")) if biblio.is_dir() else []
        self.biblio_lbl.setText(
            f"📚  Bibliothèque de darks : {len(masters)} master(s) "
            + (f"({', '.join(m.stem.replace('MasterDark_', '') for m in masters[:4])}"
               + ("…" if len(masters) > 4 else "") + ")" if masters else "— se remplit toute seule")
            + f"   ·   {biblio}")

        groupes = grouper(inv.photos["lights"], self.temp_check.isChecked(),
                          sanitize_name(nuit.name, "Cible"))
        noms = set()
        for i, g in enumerate(groupes):
            plan = preparer_plan(inv, g, noms)
            # Par défaut : la plus grosse série (les petites sont souvent des essais)
            carte = CarteCible(plan, i == 0, self._maj_bouton)
            self.cartes.append(carte)
            self.cibles_layout.addWidget(carte)
        self.cibles_hint.setText(
            "Aucune photo dans LIGHT." if not groupes else
            f"{len(groupes)} série(s) trouvée(s) — une série = même cible, même pose, même gain.")
        self._maj_bouton()

    def _plans_coches(self):
        return [c.plan for c in self.cartes if c.case.isChecked()]

    def _maj_bouton(self, *_):
        nb = len(self._plans_coches())
        self.run_btn.setEnabled(nb > 0 and self.connected and self._worker is None)
        if self._worker is None:
            self.run_btn.setText("🚀   Lancer le traitement" + (f"  ({nb} séries)" if nb > 1 else ""))

    # =====================================================================
    #  Siril
    # =====================================================================
    def _connect_to_siril(self):
        try:
            self.siril = s.SirilInterface()
            self.siril.connect()
            self.connected = True
            self.conn_lbl.setText("●  Siril connecté")
            self.conn_lbl.setStyleSheet(f"color:{VERT};")
        except SirilConnectionError as e:
            self.connected = False
            self.conn_lbl.setText("●  Siril non connecté")
            self.conn_lbl.setStyleSheet(f"color:{ROUGE};")
            QMessageBox.critical(
                self, "Connexion impossible",
                f"Impossible de se connecter à Siril :\n{e}\n\n"
                "Lance ce script depuis le menu Scripts > Scripts Python de Siril.")

    def _set_controls_enabled(self, enabled):
        for w in (self.pipeline_combo, self.dir_edit, self.browse_btn, self.nuit_btn,
                  self.temp_check, self.clean_check, *self.checkboxes,
                  *(c.case for c in self.cartes)):
            w.setEnabled(enabled)

    # =====================================================================
    #  Lancement
    # =====================================================================
    def on_run(self):
        if not self.connected or self._worker is not None:
            return
        plans = self._plans_coches()
        if not plans:
            return
        pipeline = self._current_pipeline()
        etapes_actives = {cb._step_key: cb.isChecked() for cb in self.checkboxes}

        self._set_controls_enabled(False)
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Traitement en cours…")
        self.stop_btn.setEnabled(True)
        self.log_view.clear()
        self.progress.setValue(0)
        self.resultat.setVisible(False)
        self._save_settings()
        for p in plans:
            self._append_log(f"{p.nom} : {len(p.groupe.photos)} photos — " + " · ".join(p.notes))

        self._worker = ProcessingWorker(self.siril, plans, pipeline, etapes_actives,
                                        self.clean_check.isChecked())
        self._worker.sig_log.connect(self._append_log)
        self._worker.sig_progress.connect(self._on_progress)
        self._worker.sig_phase.connect(self.phase_lbl.setText)
        self._worker.sig_done.connect(
            lambda ok, msg, reussis: self._on_done(ok, msg, reussis, plans))
        self._worker.start()

    def on_stop(self):
        if self._worker is not None:
            self._worker.request_stop()
            self.stop_btn.setEnabled(False)
            self.stop_btn.setText("Arrêt…")
            self.phase_lbl.setText("Arrêt demandé — fin de la commande en cours…")

    def _append_log(self, msg):
        self.log_view.append(msg)

    def _on_progress(self, done, total):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)

    def _on_done(self, success, message, reussis, plans):
        if self._worker is not None:
            self._worker.wait()
        self._worker = None
        self._set_controls_enabled(True)
        self.stop_btn.setText("Arrêter")
        self.stop_btn.setEnabled(False)
        nuit = plans[0].nuit
        self._analyser_dossier()      # la bibliothèque a peut-être grossi

        if reussis:
            self._montrer_resultat(nuit, reussis)
        if success:
            self.phase_lbl.setText(f"✅  Terminé : {', '.join(reussis)}")
        elif "arrêté" in message.lower():
            self.phase_lbl.setText("Arrêté.")
        elif message.startswith("Connexion perdue"):
            self.connected = False
            self.conn_lbl.setText("●  Siril non connecté")
            self.conn_lbl.setStyleSheet(f"color:{ROUGE};")
            self.phase_lbl.setText("Connexion à Siril perdue.")
            QMessageBox.critical(self, "Connexion perdue", message)
        else:
            self.phase_lbl.setText("⚠  Terminé avec des erreurs — voir le journal détaillé.")
            self.journal_btn.setChecked(True)
            QMessageBox.critical(self, "Erreur de traitement",
                                 ("Réussies : " + ", ".join(reussis) + "\n\n" if reussis else "")
                                 + "En échec :\n" + message)

    # =====================================================================
    #  Résultat
    # =====================================================================
    def _montrer_resultat(self, nuit: Path, reussis):
        nom = reussis[-1]
        self._dernier_resultat = (nuit, nom)
        self.resultat_titre.setText(f"✨  {nom}" + (f"  (+{len(reussis) - 1} autre(s))" if len(reussis) > 1 else ""))
        apercu = nuit / f"{nom}_preview.jpg"
        pix = QPixmap(str(apercu)) if apercu.is_file() else QPixmap()
        if not pix.isNull():
            largeur = max(self.resultat.width() - 44, 480)
            self.preview_lbl.setPixmap(pix.scaled(
                largeur, int(largeur * 0.68), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self.preview_lbl.setText("(pas d'aperçu)")
        self.resultat.setVisible(True)

    def _ouvrir_image(self):
        if self._dernier_resultat:
            nuit, nom = self._dernier_resultat
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(nuit / f"{nom}_processed.tif")))

    def _ouvrir_dossier(self):
        if self._dernier_resultat:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._dernier_resultat[0])))

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            reply = QMessageBox.question(
                self, "Traitement en cours",
                "Un traitement est en cours. L'arrêter et fermer la fenêtre ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.request_stop()
            self._worker.wait(5000)
        self._save_settings()
        event.accept()

    def show_about(self):
        html = f"""
        <style>
          body {{ color:#dfe3ee; font-family:'Segoe UI',Arial,sans-serif; line-height:1.5; }}
          h2 {{ color:#f4f6fc; }} h3 {{ color:#9aa8ff; margin-top:18px; }}
          code {{ background:#232838; color:#e6e9f2; padding:1px 5px; border-radius:4px; }}
          li {{ margin-bottom:5px; }}
        </style>
        <h2>OSC Studio</h2>
        <p>Choisis le <b>dossier de la nuit</b> écrit par N.I.N.A. (ex.
        <code>Astro/2026-09-23</code>). L'app lit chaque photo, retrouve les cibles
        et fait avec ce qu'il y a. <b>Seules les photos sont obligatoires.</b></p>

        <h3>Darks</h3>
        <p>Ceux de la nuit s'ils correspondent (même pose, gain, température). Sinon
        un master de la <b>bibliothèque</b> (<code>_Bibliotheque_Darks</code>, à côté
        de tes nuits). Chaque master fabriqué y est rangé : des darks faits
        <b>une fois</b> (bouchon + tissu noir, même réglages, même température)
        servent ensuite à toutes tes nuits.</p>

        <h3>Flats</h3>
        <p>Ils corrigent le vignettage (coins sombres) et les poussières. À faire
        <b>à la fin de la nuit sans toucher à la mise au point</b> : t-shirt blanc
        + écran de tablette, 20 à 30 photos, histogramme au milieu. Pas besoin de
        bias avec la SV405CC : un bias synthétique est calculé.</p>

        <h3>Couleurs</h3>
        <p>Le capteur est reconnu d'après la caméra (SV405CC → Sony IMX294) et le
        centre de la photo d'après la cible visée par N.I.N.A. Il faut Internet
        (catalogue Gaia) ; sinon l'image sort quand même, couleurs non calibrées.</p>

        <h3>Ce qui sort</h3>
        <p><code>Cible.fit</code> (linéaire, à retraiter à la main) et
        <code>Cible_processed.tif</code> (fini), dans le dossier de la nuit. Tes
        photos d'origine ne sont jamais modifiées.</p>
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("Aide")
        dlg.setMinimumSize(580, 560)
        lay = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setHtml(html)
        lay.addWidget(browser)
        fermer = QPushButton("Fermer")
        fermer.clicked.connect(dlg.accept)
        lay.addWidget(fermer)
        dlg.exec()


def main():
    app = QApplication(sys.argv)
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    win = OscStudioWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
