# Changelog

Toutes les évolutions notables de Mimir sont consignées ici.

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) ;
versionnage [SemVer](https://semver.org/lang/fr/). Les frontmatters des skills
suivent la même version (bump synchronisé).

## [Unreleased]

## [0.6.1] - 2026-06-17

### Fixed

- **`wiki-extract` ne routait pas les `.md` déposés dans `_inbox/`** (bug
  `_dev/wiki-extract-md-routing-bug-2026-06-16.md`). Les `.md` déposés
  manuellement (clip web maison, `curl` + `markdownify`, test d'un nouveau
  scraper) restaient orphelins : `doc_type_for` ne les mappait nulle part et
  `is_supported` les classait parmi les « ignorés ». Le contournement était un
  `mv _inbox/*.md raw/web/<slug>.md` manuel à chaque vague. Palier
  `route-md-inbox-notes` (specs `extract-source` et `compile-source` modifiées) :
  - Nouveau `doc_type` `notes` : `doc_type_for(".md") → "notes"`, scan `_inbox/`
    étendu aux `.md`, message d'erreur « Format non supporté » mis à jour.
  - Nouvel extracteur `extractors/markdown.py` (pass-through) : sépare
    frontmatter/corps, dérive `title` (frontmatter ou basename) et `created`
    (frontmatter ou `mtime`). Frontmatter YAML malformé ne fait plus échouer
    le lot.
  - Branche `notes` dans l'orchestrateur : écriture du triplet
    `<base>.src.md` (original verbatim, immuable) + `<base>.md` (dérivation à
    frontmatter normalisé `type: note`, `title`, `source?`, `created`,
    `tags: [clippings]`) ; move depuis `_inbox/` après écriture réussie
    (reprise sûre) ; dédup SHA de contenu (pas de dédup URL). Le slug est
    dérivé du nom de fichier, pas du frontmatter.
  - `wiki-ingest` : `_CONTENT_EXT["notes"] = ".md"` + filtre explicite
    `*.src.md` dans `scan_content` (le piège principal : sans ce filtre, la
    même note serait compilée deux fois — `.src.md` immuable + `.md` dérivé).
  - Conséquence assumée : un `README.md` parasite déposé dans `_inbox/`
    devient une note (la discipline `_inbox/` reste une responsabilité
    utilisateur). 11 nouveaux tests hermétiques ; `pytest -q` passe de 433 à
    **446 tests**.

## [0.6.0] - 2026-06-15

### Added

- **Nouveau skill `wiki-batch-produce` — production déterministe d'articles Karpathy en
  série depuis des sources web homogènes.** Adopte en repo le skill profilé battle-tested
  (REX 2026-06-08) : 1 URL = 1 notion, sans appel LLM, idempotent, avec validation
  post-écriture et rollback. 16 tests hermétiques (refus PDF/EPUB, refus frontmatter
  chapitré, refus frontmatter incomplet, source → article, idempotence (2e run = skip),
  désambiguïsation `-2`/`-3`, rollback article trop court, frontmatter flow-list,
  sérialisation, finalize par source, audit best-effort, finalize echec non-abortif,
  slug ASCII, sidecars de service ignorés, intégration ledger). Pipeline aligné sur
  le socle partagé (bootstrap unifié, confinement, NFD/NFC, slug, ledger). La suite
  passe de 6 à **7 skills** ; décompte mis à jour dans `CLAUDE.md`, `README.md`,
  `openspec/project.md`, `config.yaml`, `skills.sh.json`. Spec normative :
  `openspec/specs/batch-produce`.
- **Nouveau skill `wiki-research` — recherche en profondeur sur un sujet texte.** À partir
  d'un sujet, l'agent rassemble des sources web et les fait clipper automatiquement
  (réutilise `wiki-extract` + dédup par URL, scan récursif de `raw/web/`), puis produit un
  **rapport de synthèse** dans `reading-grids/<sujet>.md` avec frontmatter
  `type: research-report`. 16 tests hermétiques (clip loop, dédup, scaffold, renvois
  invalides, idempotence, intégration `wiki-index`). Différenciation stricte :
  `wiki-extract` (URL unique, déterministe) / `wiki-reading-grid` (PDF chapitré,
  déterministe) / `wiki-research` (sujet texte, sélection + rédaction sémantiques par
  l'agent). La suite passe de 7 à **8 skills** ; décompte mis à jour dans `README.md`,
  `SOUL.md`, `CLAUDE.md`, `config.yaml`, `openspec/project.md`, `skills.sh.json`,
  `www/index.html`, `www/en/index.html`. Spec normative :
  `openspec/specs/research-report`.
- **Intégration `wiki-index` : `type: research-report` comme entrée valide de
  `reading-grids/`.** `GridRef.kind` (`reading-grid` / `research-report`) + `GridRef.topic`
  ajoutés ; `grids_by_subject` reconnaît le nouveau type (rattaché au `subject:` de
  frontmatter) ; `build_subject_index` émet une section `## Rapports de recherche`
  dédiée. L'audit liens (lecture seule) couvre automatiquement les nouveaux rapports
  (déjà scannés via `_markdown_files`/`reading-grids/*`).

## [0.5.1] - 2026-06-09

### Changed

- **Licence : passage de propriétaire (« Tous droits réservés ») à
  [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0).**
  Usage non commercial libre (perso, recherche, éducation, assos), modifications et
  redistribution comprises ; usage commercial interdit sans accord écrit. Mise à jour du
  `LICENSE`, du champ `license` de `distribution.yaml` et des 6 `SKILL.md`
  (`PolyForm-Noncommercial-1.0.0`).

## [0.5.0] - 2026-06-08

### Added

- **Nouveau skill `wiki-init` — amorçage du vault au premier usage.** Sur un `work_root`
  vide, rien n'existait encore (pas de `_inbox/` où déposer, pas d'`INDEX.md`, pas d'état
  de synchro) ; les dossiers n'étaient créés qu'implicitement, à la première écriture, et
  l'emplacement du wiki était difficile à retrouver. Le skill pose la structure une fois,
  de façon **idempotente** et **bornée à `work_root`** :
  - **`apply`** (défaut) : crée la racine + l'arborescence (`_inbox/`, `raw/`, `wiki/`,
    `reading-grids/`), écrit un accueil `_inbox/LISEZ-MOI.md` et un `wiki/INDEX.md` initial
    **s'ils sont absents** (jamais d'écrasement), puis amorce la synchro (`pull` → `push`
    sous verrou). `--skip-sync` pour la structure seule.
  - **`status`** : diagnostic en lecture seule — imprime le `wiki.config.json` résolu et
    tous les chemins dérivés, en signalant ce qui existe/manque (répond à « où est mon
    wiki »).
  `.wiki/` (ledger) reste créé à l'ingestion ; l'index complet se régénère via
  `wiki-index regenerate`. 8 tests hermétiques (création, idempotence, confinement,
  amorçage sync via runner mocké). SOUL.md (étape 0), README (« Première utilisation »)
  et CLAUDE.md mis à jour. La suite passe de 5 à **6 skills**.
- **Chapitrage alphanumérique dans `wiki-reading-grid`.** L'ancre `#chK` accepte désormais un
  **numéro** (`#ch3`) **ou un code d'ouvrage** (`#chR2`, `#chG3`, `#chC1`), `K` devant matcher
  le champ `order` du `.toc.json`. L'**ordre des chapitres dans le `toc.json` fait foi** (la
  grille ne retrie plus : trier des codes alphabétiquement cassait l'ordre canonique). Helper
  `_chapter_key` (normalisation `"03"→"3"`, `"g3"→"G3"`). Rétrocompatible avec le numérique.
- **Suggestion de slug ASCII dans l'audit `wiki-index`.** Un lien cassé accentué
  (`[[réglementation/relèvement]]`) propose la forme existante slugifiée
  (`reglementation/relevement`) — aide read-only, sans légitimer les slugs non-ASCII.

### Changed

- **README rafraîchi.** Le bandeau « projet en construction / tout reste à créer » laisse
  place à l'état réel (pipeline 6 skills en marche), avec une section « Comment ça marche »,
  un encart « langue » assumant le français, et un appel aux retours. Le lien interne mort
  vers `_analyse_DevOps/` (jamais publié) est retiré.

### Fixed

- **Mise à jour du profil documentée.** Sur un profil déjà installé, relancer `hermes
  profile install` échoue (« already exists ») : la mise à niveau se fait via `hermes
  profile update mimir` (en place) ou `hermes profile install … --force`. ⚠️ `update`
  prend le **nom du profil** (`mimir`), pas l'URL git — passer l'URL échoue (« Profile
  '…' is not a distribution »). README + runbook de déploiement (§6) précisés.
- **Grille à « 0 article lié » après ingestion (cause racine).** Le ledger stockait des
  wikilinks préfixés `wiki/` que `article_index.load_article` re-concaténait à `cfg.WIKI`
  (`wiki/wiki/…`, introuvable → tous les articles orphelins). `wiki-ingest finalize`
  canonicalise désormais en `sujet/notion` (sans préfixe) et `load_article`/`_find_article`
  tolèrent un préfixe résiduel. Re-`finalize` redevient idempotent.
- **Run d'extraction interrompu par un intrus dans `_inbox/`.** Un `README.md`/`.txt` atteignait
  `doc_type_for` (appelé hors `try`) et faisait crasher tout le batch (non-ingestion silencieuse
  en cron). `scan_inbox` ne retient que `.pdf`/`.epub` et signale les fichiers ignorés ;
  `doc_type_for` est passé sous `try`. Helper `extractors.is_supported`.

### Notes

- **Le « bug regenerate écrase le ledger » n'existait pas** : le ledger n'est écrit que par
  `wiki-ingest finalize` ; `wiki-index regenerate` n'y touche jamais. Le symptôme était une
  rechute du préfixe `wiki/` lors d'un re-`finalize`. Démystifié dans `CONVENTIONS.md` §9.
- **Le SKILL.md n'imposait pas la convention `#chK` (R1/G3)** : elle a été déduite par l'agent
  face au guide. Les deux conventions sont désormais explicitement documentées et supportées.

## [0.4.0] - 2026-06-07

### Added

- **Synchro kDrive auto-amorçable depuis le JSON (`wiki-sync` / backend rclone).** Deux
  clés optionnelles dans `sync.rclone` (rétro-compatibles, défaut = comportement actuel
  inchangé) suppriment les actes `rclone` manuels sur une machine pilotée par config :
  - **`auto_resync`** : sans état bisync antérieur (« Must run --resync »), le backend
    amorce lui-même via `bisync --resync` — **union sans perte** (pas de `--resync-mode` ;
    `--conflict-suffix sync-conflict` conservé, donc un fichier divergent des deux côtés
    produit deux copies au lieu d'un écrasement silencieux). Échec ⇒ repli sur le
    bootstrap `rclone sync` unidirectionnel existant (filet de sécurité intact).
  - **`remote_setup`** (`url` / `vendor` / `user` / `pass_env`) : crée/répare le remote
    rclone (`config create` / `update --obscure`) **avant** la synchro ; le mot de passe
    est lu dans la variable d'environnement nommée par `pass_env` (**jamais stocké dans le
    JSON**), et un remote déjà présent voit son secret rafraîchi — **auto-réparation du
    401**.
  Schéma `wiki.config.schema.json` étendu, `wiki.config.example.json` + référence
  `RCLONE_KDRIVE.md` documentés (dont le caveat « secret transmis via `argv` »), `filters.txt`
  nettoyé (note perso mode A en exemple commenté). 7 tests hermétiques ajoutés (runner factice,
  aucun binaire rclone requis).

## [0.3.5] - 2026-06-07

### Added

- **Documentation du premier lancement (`hermes setup`).** Section `## Installation`
  au README + walkthrough complet de l'assistant au runbook §5 : provider (Nous
  Portal / clés), terminal, réglages agent, messagerie Telegram (allowlist + home
  channel), **service gateway launchd** `ai.hermes.gateway-mimir.plist`, et résumé des
  outils (quelle clé débloque quelle catégorie).

### Fixed

- **Invocation standardisée sur `hermes -p mimir <cmd>`.** Tous les `mimir <cmd>`
  (qui supposaient un alias) convertis : `hermes profile install` **ne crée pas**
  l'alias `~/.local/bin/mimir` sans `--alias`. Avertissement ajouté : `hermes mimir
  chat` est invalide (`mimir` = profil, pas sous-commande).
- **Gateway : plus « prévu en v0.4.0 ».** Son service launchd est installé/démarré dès
  l'assistant `hermes setup` ; seule la synchro planifiée (cron) reste en v0.4.0.
  Corrigé dans le runbook §5 et la SPEC §11.
- **Modèle « hérité » nuancé.** L'héritage ne vaut que si le profil par défaut a déjà
  un provider ; sinon le 1er `hermes -p mimir chat` lance `hermes setup`.
- **Skills de base manquantes après `hermes profile install` (doc corrigée).**
  `hermes profile install` ne copie que les *distribution-owned files* (les 5
  wiki-*) — il **ne seede PAS** les bundled skills de base (seuls `hermes profile
  create` les seede, et `hermes update` les synchronise dans tous les profils).
  Correction des affirmations erronées d'« héritage automatique » des skills
  (`config.yaml`, `SPEC.md` §11, `RUNBOOK_VALIDATION_HERMES.md` §5, `SOUL.md`) et
  ajout de l'étape post-install **`hermes update`** (« syncs new bundled skills to
  all profiles » → `mimir (+N new)`). Le modèle LLM, lui, reste bien hérité.

## [0.3.4] - 2026-06-07

### Changed

- **`distribution.yaml` conformé au schéma officiel Hermes.** Retrait de la clé
  `files:` (inexistante — Hermes copie tout le repo dans le profil) ; ajout de
  `license` et `distribution_owned` (`SOUL.md`, `config.yaml`, `skills/`).
  (Corrige la v0.3.3 qui embarquait encore le `files:` non conforme.)
- **`config.yaml` minimal (héritage).** Aucun bloc `model:` : le profil **hérite**
  du modèle du profil par défaut (sinon `mimir setup`). Retrait des blocs
  spéculatifs `delegation:`/`skills.hub:` (non confirmés par la doc officielle) et
  du `MIMIR_CONFIG` placeholder (inutile : `wiki.config.json` est auto-découvert
  dans le dossier du profil). SOUL/SPEC/runbook clarifient que « N compétences » ne
  compte que les skills custom, et documentent les étapes post-install (modèle,
  gateway).

### Removed

- **Poids mort interne supprimé** (jamais publié) : dossier `_old-kb-mimir-skills/`
  (anciens skills `kb-*`, remplacés par les `wiki-*` ; historique conservé par git)
  et les 3 notes de recherche Hermes (`hermes-sous-agents-vs-skills.md`,
  `hermes-remontee-modifications-auto-git.md`, `guide-devops-hermes-wiki-gitlab.md`)
  — potentiellement inexactes ; **source de vérité = doc officielle
  NousResearch/hermes-agent**. Conservés : les 3 `RUNBOOK_*.md` + le diagramme.

## [0.3.3] - 2026-06-07

### Added

- **Auto-découverte de la config dans le dossier du profil Hermes.** En plus de
  `$MIMIR_CONFIG`, `~/.config/mimir/` et `./`, le resolver cherche désormais à la
  **racine du profil/repo** : `~/.hermes/profiles/<profil>/wiki.config.json` (en
  prod) ou la racine du repo (en dev), déduite de l'emplacement de
  `config_loader.py` (`_self_root()`, `parents[3]`). Ordre :
  `$MIMIR_CONFIG` → **profil** → XDG → `./`. La config du profil prime sur la
  globale. UX : déposer son `wiki.config.json` dans le dossier du profil installé
  (p. ex. copier `wiki.config.example.json` → `wiki.config.json`) suffit — plus
  besoin de `MIMIR_CONFIG`.

### Changed

- **Profil Hermes renommé `wiki-curator` → `mimir`.** Le `distribution.yaml`
  porte `name: mimir` ; l'install crée `~/.hermes/profiles/mimir` et l'usage
  devient `hermes -p mimir chat`. Aligné sur l'identité produit (et la mythologie :
  Mímir = le sage gardien du savoir). Indices `metadata.hermes.profile` des 5
  skills + `SOUL.md`/`config.yaml`/SPEC/ROADMAP mis à jour.
  ⚠️ Après réinstallation, supprimer l'ancien profil : `rm -rf ~/.hermes/profiles/wiki-curator`.
- `.gitignore` ignore `/wiki.config.json` (la racine du repo est un emplacement de
  découverte en dev ; la config perso ne doit jamais être committée).

## [0.3.2] - 2026-06-07

### Fixed

- **`distribution.yaml` au vrai format Hermes.** Le manifest utilise désormais le
  schéma **plat** documenté (`name`/`version`/`description`/`author`/`files`) au
  lieu de la structure `apiVersion/kind/metadata/…` (qui causait
  `Error: distribution.yaml missing 'name'` à `hermes profile install`).
- **`SOUL.md` + `config.yaml` remontés à la racine du repo.** `profile install`
  copie les `files:` dans `~/.hermes/profiles/wiki-curator/` ; un profil attend
  `SOUL.md`/`config.yaml` à la racine de son dossier, donc ils doivent être listés
  à plat. Dossier `profiles/` supprimé. Allow-list de publication ajustée
  (`SOUL.md`, `config.yaml` ajoutés ; `profiles/` retiré).

## [0.3.1] - 2026-06-07

### Fixed

- **`hermes profile install` réparé.** Le manifest `distribution.yaml` est déplacé
  de `profiles/wiki-curator/` vers la **racine du repo** (requis : `profile install`
  ne supporte pas la syntaxe `#sous-dossier` et cherche le manifest à la racine).
  Chemins `soul`/`config` → `profiles/wiki-curator/`, `assets` → racine. Ajouté à
  l'allow-list de publication. Commande : `hermes profile install https://github.com/vivian-maes/mimir.git`.

### Changed

- **Réorganisation : gestion de projet sous `__projet__/`.** `ROADMAP.md`,
  `BACKLOG.md`, `SPEC.md` et les dossiers `_analyse_DevOps/`, `_old-kb-mimir-skills/`
  sont regroupés sous `__projet__/` (jamais publié — ajouté au garde-fou anti-fuite).
  Racine allégée ; artefacts publiés (README, CHANGELOG, VERSION, LICENSE, configs,
  `distribution.yaml`, `skills.sh.json`) conservés à la racine. Liens internes mis à jour.

## [0.3.0] - 2026-06-07

> Configuration « zéro friction » : on configure **une fois**, on appelle les
> skills **sans** `--config`. Exposition au Skills Hub d'Hermes (regroupement +
> bouton config) et install groupée par le profil mise en avant.

### Added

- **Auto-découverte du `wiki.config.json`.** `--config` devient **optionnel** sur
  les 5 skills. Quand il est omis, le config est résolu dans l'ordre
  `$MIMIR_CONFIG` → `~/.config/mimir/wiki.config.json` (XDG) → `./wiki.config.json`
  (`config_loader.resolve_config_path` / `load_resolved_config`). Message d'erreur
  actionnable si rien n'est trouvé. Plus besoin de répéter le chemin à chaque appel.
- **Skills Hub.** Frontmatters enrichis (`metadata.hermes.category`,
  `related_skills`, `config`) → les 5 skills apparaissent comme un **ensemble**
  avec un **bouton config**. Nouveau `skills.sh.json` (catégorie « Mimir — Second
  cerveau »), ajouté à l'allow-list de publication.

### Changed

- **Profil = voie d'install groupée recommandée.** `hermes profile install https://github.com/vivian-maes/mimir.git`
  (les 5 skills + config + cron d'un coup) mis en avant dans les runbooks. Le
  manifest **`distribution.yaml` est à la racine** du repo (requis par
  `profile install` ; pas de syntaxe `#sous-dossier`) ; `SOUL.md` + `config.yaml`
  restent sous `profiles/wiki-curator/`. Le `config.yaml` exporte `MIMIR_CONFIG`
  (appels sans `--config`) ; le cron garde `--config` explicite (sessions sans `cwd`).
- **CI tag-only.** Plus aucun pipeline hors tag (`workflow.rules` + suppression
  du job de validation au push) : la validation (frontmatters + 159 tests) et la
  publication ne tournent qu'au tag.
- **Publication GitHub incrémentale.** `scripts/publish_github.sh` n'écrase plus
  l'historique (`--force`) : il hérite de `origin/main` et ajoute **un seul
  commit par version** ne contenant **que le diff** (message `Release vX.Y.Z` +
  section CHANGELOG). Garde-fou anti-fuite conservé : les fichiers internes
  (`_analyse_DevOps/`, `_old-kb-mimir-skills/`, `ROADMAP.md`, `SPEC.md`,
  `.gitlab-ci.yml`) n'entrent jamais — ni en fichiers, ni en historique.

## [0.2.0] - 2026-06-05

> Première release packagée : les phases 0→4 sont livrées (les 5 skills `wiki-*`
> opérationnels). `VERSION` aligné sur les frontmatters des skills (tous à `0.2.0`).

### Added

- **Phase 5 — Packaging & déploiement Hermes** (amorce). Publication GitHub
  **filtrée par allow-list** (`scripts/publish_github.sh`) : seuls `skills/`,
  `README.md`, `VERSION`, `CHANGELOG.md`, `LICENSE`, `wiki.config.example.json`,
  `filters.txt`, `assets/` et `profiles/` partent vers le miroir GitHub ; les
  fichiers internes (`_analyse_DevOps/`, `_old-kb-mimir-skills/`, `ROADMAP.md`,
  `SPEC.md`, `.gitlab-ci.yml`) **ne sont jamais transmis** (la push-mirror native
  GitLab est écartée car elle ne sait pas filtrer). Publication **incrémentale**
  (un commit par version, diff seul ; pas de réécriture d'historique).
  `.gitlab-ci.yml` **tag-only** : `validate` (frontmatters + 159 tests) puis
  `publish:github`, sur tag uniquement. Profil opt-in **`wiki-curator`** (couche 2)
  : `profiles/wiki-curator/` (`distribution.yaml` + `SOUL.md` + `config.yaml` +
  cron de synchro) — jamais requis, les skills restent portables hors profil.

- **Phase 4 — Synchronisation pluggable** (`wiki-sync` v0.2.0). Moteur de synchro **pluggable**
  promu dans le socle partagé `_shared-references/scripts/sync/` : ABC `SyncBackend`
  (`lock`/`pull`/`push`/`validate`) + factory `get_backend(cfg, runner)` sélectionnant
  `rclone | git | noop` selon `sync.backend`, avec `runner=subprocess.run` **injectable**
  (tests 100 % hermétiques, aucun binaire requis). Façade `sync.pull/push/validate/lock(cfg)`
  importée par les 4 orchestrateurs. **Backend rclone** (`sync/rclone.py`) : `bisync --size-only`
  (+ `--conflict-loser pathname`, `--max-delete 25`, `--resilient --recover`, `--filter-from`),
  fallback `sync` unidirectionnel au premier run / état bisync perdu (jamais `--resync` auto),
  `validate` **anti-listing-stale** (comptage local vs `rclone lsf`). **Backend git**
  (`sync/git.py`) : `pull --rebase`, `add`/`commit`/`push` **scopé** (`_inbox/`, `raw/`, `wiki/`,
  `reading-grids/`), `validate` = `HEAD` local vs `origin/<branch>`. CLI `wiki_sync.py`
  (`lock|pull|push|validate|sync`, `--dry-run`) pour le trigger « synchronise le vault ».
  `SKILL.md` opérationnel + `references/{RCLONE_KDRIVE,GIT_BACKEND}.md` (savoir kDrive porté
  depuis l'ancien `kb-sync`). 32 tests pytest (factory/noop/lock + git e2e local + rclone mocké
  + CLI). DoD : cycle `pull → push → validate` vert sur git (réel) et rclone (mocké), verrou
  hors zone synchronisée empêchant la concurrence.

- **Phase 3 — Grille de lecture & index** (`wiki-reading-grid` v0.2.0, `wiki-index` v0.2.0).
  Deux skills **100 % déterministes** (aucun travail sémantique d'agent). `wiki-reading-grid` :
  `wiki_reading_grid.py` (`generate` / `generate-all`) + `grid_builder.py` qui croise
  `toc.json` (chapitres ordonnés) × ledger (articles d'un ouvrage) × ancres `#chK` des `sources`
  pour produire `reading-grids/<slug>.md` — liens **ordonnés par chapitre** + **Précédent/Suivant**
  (titre et lien dérivés d'une même fonction `chapter_anchor` ⇒ aucun lien interne cassé), MAJ de
  la colonne « Grille » de `_status.md`, idempotent (`created` préservé), slug dérivé du titre
  (désambiguïsation `-2`), web sans chapitrage ignoré. `wiki-index` : `wiki_index.py`
  (`regenerate` / `audit`) + `index_builder.py` (INDEX principal des sujets avec **description
  éditoriale préservée**, INDEX par sujet avec résumés issus du callout `>` + grilles rattachées,
  **forme majoritaire** des wikilinks) + `link_audit.py` (**audit lecture seule, 3 passes** :
  liens cassés / fichiers fantômes / index→vide, normalisation `os.path.normpath`, code retour
  0 ssi 0 anomalie). Nouveaux helpers partagés : `frontmatter.py` (parsing promu depuis
  `article_writer.py`), `article_index.py` (lecture d'article + résumé), `wikilinks.py`
  (extraction / forme majoritaire / résolution / ancrage chapitre). 35 tests pytest (helpers +
  2 skills), DoD vérifié (grille suit le chapitrage sans lien cassé ; audit à zéro sur vault sain).

- **Phase 2 — Compilation → wiki** (`wiki-ingest` v0.2.0). Modèle **agent-piloté + helpers** :
  l'agent décompose et reformule (méthode Karpathy), le Python ne fait que le déterministe.
  CLI fin `wiki_ingest.py` à trois sous-commandes (`inventory` → worklist des sources à compiler
  via diff ledger ; `write-article` → écrit/écrase un article notion ; `finalize` → MAJ `_status.md`
  + ledger). Nouveau **ledger** partagé `.wiki/ingest-ledger.json` (`ledger.py` : atomique tmp+rename,
  corrompu→`{}`, hors sync) pour l'**idempotence**. `article_writer.py` : frontmatter Karpathy,
  **antidoublon = remplacer le corps** + fusion frontmatter (`created` conservé, `sources`/`tags`
  en union), `_assets` par sujet, validation NFD/NFC. `inventory.py` (SHA + exclusions des
  sidecars de service). `sync_stub.py` (interface `pull`/`push`/`validate` du futur `wiki-sync`,
  no-op ; `--skip-sync`). 22 tests pytest.

- **Phase 1 — Extraction → raw** (`wiki-extract` v0.2.0). Orchestrateur CLI
  `wiki_extract.py` + dropzone **`_inbox/`** (synchronisée, verrouillée) ; 3 extracteurs
  pluggables (`pdf`, `epub`, `web`) sur contrat commun `extract() → ExtractResult`, chacun
  avec dégradation gracieuse (lib riche → fallback CLI/stdlib) et **jamais d'invention** de
  contenu (OCR auto PDF, sinon `ExtractorUnavailable`). Helpers partagés `iohelpers.py`
  (écriture atomique confinée, suffixage `-2`, SHA) et `status_table.py` (`_status.md`).
  Immutabilité de `raw/`, déduplication par SHA de contenu. Clé `inbox` ajoutée au layout de
  config. `wiki-extract/requirements.txt`. 57 tests pytest.

- **Phase 0 — Fondations.** Arborescence mono-repo `skills/` (5 skills `wiki-*` +
  `_shared-references/`).
- Socle Python partagé : chargeur `wiki.config.json` (`config_loader.py`), garde de
  confinement à `work_root` (`guard.py`), slugs ASCII + NFD/NFC (`slug.py`),
  validateur de frontmatter agentskills.io (`validate_skills.py`).
- Schéma JSON `wiki.config.schema.json` + exemple `wiki.config.example.json`.
- Conventions partagées : `CONVENTIONS.md`, `FRONTMATTERS.md`, `TOOLING.md`.
- Squelettes des 5 `SKILL.md` (frontmatter conforme, corps non fonctionnel).
- Tests pytest (résolution config modes A/B, confinement).
- Amorces packaging : `VERSION`, `CHANGELOG.md`, `.gitlab-ci.yml` (validation
  déclenchée uniquement sur tag), `filters.txt` (template rclone).

### Changed

- Le verrou `wiki-extract/inbox_lock.py` (Phase 1) est **promu** dans le socle partagé
  (`sync/locking.py`) et réutilisé par tous les backends et orchestrateurs ; l'ancien nom de
  classe `InboxLock` reste disponible en alias. `wiki-extract` câble désormais `lock → pull →
  extract → push` (flag `--skip-sync`). Défaut de scope git du schéma : `_inbox` inclus (SPEC §6).

### Removed

- Les trois copies dupliquées de `sync_stub.py` (`wiki-ingest`, `wiki-reading-grid`,
  `wiki-index`) et `wiki-extract/inbox_lock.py` — remplacées par le moteur partagé `sync/`.

[Unreleased]: https://github.com/vivian-maes/mimir/compare/v0.5.1...HEAD
[0.6.0]: https://github.com/vivian-maes/mimir/releases/tag/v0.6.0
[0.5.1]: https://github.com/vivian-maes/mimir/releases/tag/v0.5.1
[0.5.0]: https://github.com/vivian-maes/mimir/releases/tag/v0.5.0
[0.2.0]: https://github.com/vivian-maes/mimir/releases/tag/v0.2.0
