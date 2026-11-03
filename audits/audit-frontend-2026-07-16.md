# Audit frontend — analyseur_rapports

Date : 2026-07-16  
Périmètre : accessibilité, performance, sécurité UI, UX, CSS/JS, responsive  
Référence backend : `audits/audit-backend-2026-07-15.md`

## 1. Synthèse exécutive

Le frontend est une interface **multi-apps Flask** (templates Jinja + CSS/JS vanilla partagés) destinée à un usage **local / intranet**. L’expérience visuelle est cohérente (thème sombre, polices Distinctives, topbar commune), et plusieurs bonnes pratiques de sécurité UI sont déjà en place (CSRF côté client, `textContent` pour les toasts, en-têtes CSP).

En l’état, l’UI reste **correcte pour un outil métier interne**, mais présente des freins concrets :

- **navigation mobile absente** (liens complètement masqués) ;
- **styles inline massifs** qui bloquent une CSP stricte et compliquent la maintenance ;
- **copie BDC obsolète** (invite à saisir un nom de fichier inexistant) ;
- **accessibilité partielle** (toasts, barre de progression, accordéon, motion) ;
- **perf dépendante de Google Fonts** et de CSS/JS non cacheés côté headers.

État global : **acceptable en local**, **à remédier avant usage intensif multi-opérateurs ou exposition hors machine contrôlée**.

## 2. Périmètre audité

### 2.1 Surfaces UI

| Surface | Rôle | Stack UI |
|---|---|---|
| Convertisseur BDC (`:5000`) | Lancer conversion PDF → JSON | `base.html` + `bdc_convert.js` |
| Reporting (`:5010`) | Analyse mensuelle + export | `base.html` + `reporting.js` |
| Réconciliation (`:5030`) | Comparer exports + export HTML/JSON | `base.html` |
| Export HTML reporting | Document imprimable | `report.html` (autonome) |
| Export HTML rapprochement | Document imprimable | `export.html` (autonome) |
| Dashboard services (`:5500`) | Start/stop services | template string + `dashboard.js` |
| Page auth admin | Saisie `ADMIN_TOKEN` | template string dans `libs/web/auth.py` |

### 2.2 Fichiers examinés

- `apps/templates/base.html`
- `apps/static/css/global.css`
- `apps/static/js/global.js`
- `apps/static/js/bdc_convert.js`
- `apps/static/js/reporting.js`
- `apps/static/js/dashboard.js`
- `apps/bdc_converter/templates/index.html`
- `apps/reporting/templates/index.html`
- `apps/reporting/templates/report.html`
- `apps/reconciliation/templates/index.html`
- `apps/reconciliation/templates/export.html`
- `scripts/service_dashboard.py` (template HTML inline)
- `libs/web/auth.py` (page login)
- `libs/web/headers.py` (CSP / headers sécurité)

### 2.3 Méthode

Revue statique du code UI (HTML/CSS/JS), croisement avec les headers de sécurité partagés, inventaire des patterns d’accessibilité et de perf. Pas de session manuelle instrumentée (Lighthouse / lecteur d’écran) dans cette passe.

## 3. Architecture frontend observée

```text
apps/templates/base.html
  ├── /static/css/global.css
  ├── /static/js/global.js   (App.showToast, CSRF helpers)
  └── pages app-specific
        ├── bdc_converter/templates/index.html + bdc_convert.js
        ├── reporting/templates/index.html + reporting.js
        └── reconciliation/templates/index.html

Exports autonome (sans base.html) :
  reporting/report.html
  reconciliation/export.html

Dashboard (hors apps/) :
  scripts/service_dashboard.py → render_template_string + dashboard.js
```

Points d’architecture :

- **Bon** : assets partagés sous `apps/static/`, helpers JS centralisés (`window.App`).
- **Fragile** : navigation hardcodée vers `http://127.0.0.1:{5000,5010,5030}`.
- **Duplication** : tokens de design et styles réimplémentés dans `report.html`, `export.html` et le dashboard.
- **Volume** : ~1,8 kLOC UI (hors venv) ; CSS global seulement ~364 lignes — le reste est surtout **inline dans Jinja**.

## 4. Points positifs

- Language `lang="fr"` sur les documents principaux.
- Viewport présent sur `base.html` et `report.html`.
- Design system embryonnaire via variables CSS (`--accent`, `--muted`, `--bg-*`).
- CSRF exposé en meta + injection form + `App.authHeaders()` pour les `fetch` POST.
- Toasts construits en DOM avec `textContent` (pas d’injection HTML).
- Liens externes (`target="_blank"`) avec `rel="noopener"` / `noreferrer`.
- Quelques micro-intentions a11y : `aria-live` sur la progression BDC, `aria-expanded` sur la synthèse reporting.
- Exports HTML pensés pour l’impression (`@media print`, contraste clair).
- Headers sécurité côté serveur déjà présents (`CSP`, `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`).
- Pas de jQuery / framework lourd ; JS léger et lisible.

## 5. Constats par domaine

Légende : **P0** bloquant / trompeur · **P1** important · **P2** amélioration

---

### 5.1 Accessibilité (a11y)

#### P0 — Navigation principale inaccessible sur mobile

Dans `global.css` :

```css
@media (max-width: 768px) {
  .topbar__links { display: none; }
}
```

Aucun menu alternatif (burger, drawer, select, liens en pied de page). Sur viewport ≤768px, l’utilisateur **ne peut plus basculer** entre BDC / Reporting / Réconciliation.

#### P1 — Toasts non annoncés aux technologies d’assistance

`#toast-container` n’a ni `role="status"` / `aria-live="polite"` ni `aria-atomic`. Les succès/erreurs de conversion ou génération JSON restent visuels uniquement.

#### P1 — Barre de progression incomplète sémantiquement

Le conteneur a `aria-live` / `aria-label`, mais `#progressBar` n’expose pas `role="progressbar"`, `aria-valuemin/max/now`. Le libellé textuel compense partiellement, mais une AT incompletes.

#### P1 — Accordéon synthèse : `aria-controls` manquant

Le bouton `#toggleSummary` gère `aria-expanded` mais ne référence pas `#summaryContent` via `aria-controls`. Le chevron « ▶ » est purement visuel (acceptable s’il est décoratif, mais l’état devrait aussi être verbalisé).

#### P1 — `outline: none` sur focus clavier

Les champs ont un anneau via `box-shadow` au `:focus`, ce qui est bien. Les boutons génériques (`button, .btn`) n’ont **pas** de style `:focus-visible` dédié — risque de perte d’indice de focus hors souris.

#### P1 — Motion sans `prefers-reduced-motion`

Animations (`fadeIn`, `slideInRight`, hover `translateY`, transitions topbar) toujours actives. À réduire/désactiver pour les utilisateurs sensibles au mouvement.

#### P2 — Landmarks / navigation

- `<nav class="topbar">` sans `aria-label`.
- SVG marque décorative sans `aria-hidden="true"`.
- Pas de lien « Aller au contenu ».
- Brand « Camille Reporting » non cliquable (pas de retour accueil clair).

#### P2 — Tableaux de réconciliation

Tables d’écarts sans `<caption>` ni `scope` sur les en-têtes. Compréhensibles visuellement, moins optimales pour lecteurs d’écran.

#### P2 — Page auth

Le bouton utilise `class="btn"` sans variante primaire visible dans `global.css` (`.btn` de base seulement + `.btn-primary`). Contraste / hiérarchie visuelle plus faible que les autres écrans. Message d’erreur en couleur `#b00020` hors design tokens.

#### P2 — Dashboard / export : viewport

- Dashboard : **pas de** `<meta name="viewport">`.
- `export.html` : **pas de** viewport (contrairement à `report.html`).

#### Contraste (estimation)

Paire `--muted` (`#9aa4b8`) sur `--bg-main` (`#080b11`) : a priori conforme AA pour texte normal. Paires accent / fonds semi-transparents OK en usage courant. À valider avec un outil de mesure sur composants réels (pills, disabled, liens muted).

---

### 5.2 Performance

#### P1 — Polices Google en bloquant réseau

`base.html` charge Plus Jakarta Sans + Space Grotesk via `fonts.googleapis.com` / `fonts.gstatic.com`.  
Points :

- dépendance externe (latence, disponibilité, vie privée) ;
- le CSP autorise déjà ces origines ;
- `display=swap` est en place (bon) ;
- le **dashboard** charge les fonts via `@import` dans un `<style>` (pire pattern : cascade de requêtes).

Recommandation : self-host (woff2) + `font-display: swap` local, ou subset.

#### P1 — Styles inline massifs

Exemples de densité `style="..."` :

- `reconciliation/templates/index.html` : ~70 attributs inline ;
- `reporting/templates/index.html` : ~36 ;
- exports et dashboard : gros blocs `<style>` dupliqués.

Impact : HTML plus lourd, pas de cache CSS mutualisé, CSP forcée à `style-src ... 'unsafe-inline'`.

#### P2 — Polling dashboard toutes les 4 s

`dashboard.js` : `setInterval(refreshStatuses, 4000)` sans backoff, sans `document.visibilityState`, sans SSE/WebSocket. Acceptable en local à 3 services ; inutilement chatty si laissé ouvert longtemps.

#### P2 — Progression BDC fictive

Animation aléatoire jusqu’à 90 % puis salto à 100 %. Pas de coût serveur, mais trafic cognitif (UX) et timers inutiles si l’API répond vite.

#### P2 — Pas de politique de cache sur `/static`

Aucun `Cache-Control` / hash de fingerprint visible pour CSS/JS. En local ce n’est pas critique ; en intranet multi-utilisateurs, les assets ne bénéficient pas de cache longue durée.

#### P2 — Effets CSS coûteux

`backdrop-filter: blur(...)` sur topbar et panels. Sur machines modestes / GPU partagé, peut provoquer du jank au scroll. Prévoir un fallback opaque.

#### P2 — Meta hors `<head>`

Dans reporting, `<meta name="selected-month">` est injecté dans `{% block extra_js %}` (corps de page). Navigateur le tolère souvent, mais ce n’est pas valide HTML5 et retarde éventuellement la découverte.

---

### 5.3 Sécurité UI

#### Déjà en place (positifs)

- Token CSRF en meta + header `X-CSRFToken` sur les mutations JS.
- Champs hidden CSRF sur formulaires POST reporting / réconciliation / auth.
- CSP par défaut (`libs/web/headers.py`) : `script-src 'self'`, `connect-src 'self'`, `frame-ancestors 'none'`, `form-action 'self'`.
- Pas d’`eval`, pas de handlers `onclick=` inline dans les templates app.
- Toasts / logs construits avec `textContent` / `createElement`.

#### P1 — CSP affaiblie par le design actuel

```text
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com
```

Tant que les templates reposent sur des `style=""` et des `<style>` pages, on ne peut pas resserrer la CSP. Pas de nonces/hashes pour le CSS. Les scripts externes sont correctement limités à `'self'`.

#### P1 — Token d’export imprimé en clair

`export.html` affiche `Token: {{ meta.token }}`. Ce token porte l’accès aux exports JSON/HTML en mémoire. Un PDF/impression/copie d’écran le divulgue. À retirer de la vue utilisateur (ou masquer, le garder uniquement en URL temporaire côté serveur).

#### P1 — URLs absolues cross-origin hardcodées

Topbar :

```html
<a href="http://127.0.0.1:5000/">...</a>
```

Risques UI/sécu :

- casse si bind sur autre host / reverse-proxy / HTTPS ;
- mélange d’origines (cookies CSRF/admin **par port** — comportement attendu multi-process, mais navigation confuse) ;
- surface différente de `connect-src 'self'` (navigation pleine page OK, mais pas unifiable sous un seul origin).

#### P2 — Gestion d’erreur fetch incomplète

- `bdc_convert.js` : `response.json()` même si corps non-JSON → exception générique (correctement catchée, OK).
- `dashboard.js` : aucune vérification `response.ok` ; échec silencieux hors `console.error` ; pas de toast.
- `reporting.js` : mélange `App.showToast` / `window.App` ; échoue si `global.js` absente.

#### P2 — Contenu utilisateur dans le DOM

Noms de fichiers / lots issus des données métier sont rendus via Jinja (échappement auto — bon). Les listes construites en JS utilisent `textContent` — bon. Seul `progressLog.innerHTML = ""` (clear) : faible risque.

#### P2 — Emoji dans le texte de lien

`📄 Ouvrir le rapport` : parfois mal vocalisé ; préférer une icône SVG `aria-hidden` + texte clair.

---

### 5.4 UX / produit

#### P0 — Copie BDC trompeuse

Texte actuel :

> « Saisissez un nom de fichier avant de lancer la conversion… »

Or l’UI **n’a plus de champ** nom de fichier : le serveur force `convert_bdc_MM-YYYY.json` (confirmé README + `app.py`). Instruction obsolète → confusion opératoire.

#### P1 — Pas de feedback métier sur le dashboard

Start/stop : erreurs seulement en console. L’opérateur ne voit pas « Service déjà lancé », échec `make`, etc. Les toasts `App.showToast` existent mais ne sont pas utilisés.

#### P1 — États vides / erreurs

- Réconciliation : messages si aucun rapport — bon.
- Reporting : si `months` vide, `<select required>` potentiellement vide → submit impossible sans message explicite.
- Alertes serveur présentes (`.alert--error`) — bon pattern à généraliser.

#### P1 — Progression non réelle (BDC)

La barre suggère un avancement réel alors qu’elle est cosmétique jusqu’à la réponse. Préférer indéterminé (`aria-busy`) ou brancher le `progress` renvoyé par l’API (déjà affiché en liste après coup).

#### P2 — Cohérence de navigation

- Topbar brand non cliquable.
- Dashboard hors topbar commune (expérience « suite » cassée).
- Exports HTML sans retour vers l’app (normal pour pièce jointe, mais pas de fil d’Ariane côté écrans live).

#### P2 — Densité informationnelle réconciliation

Beaucoup de cartes / tables ; lecture OK sur desktop, fatigante sur petit écran. Manque filtre « écarts seulement » ou résumé sticky des `issues_count`.

---

### 5.5 Qualité CSS / JS

#### P1 — Design system sous-exploité

`global.css` définit panels, boutons, alerts, toasts, progress — mais les pages re-stylent via inline au lieu d’utilitaires (grids, stats, pills, steps). Conséquence : divergences dashboard vs apps, dette visuelle.

#### P1 — Duplication des thèmes d’export

`report.html` et `export.html` recopient tokens + print CSS. Toute évolution de marque doit être faite ×3 (+ dashboard).

#### P2 — Spécificité / hygiène CSS

- `color` dupliqué dans `.topbar__links a.active`.
- `!important` sur `.text-positive` / `.text-negative`.
- Sélecteurs globaux forts (`button, .btn`) — risque de collatéraux.
- Auth page hors topbar : padding `body` 70px inutile (pas de topbar) → décalage vertical.

#### P2 — JS global namespace léger mais perfectible

- `window.App` : pattern simple et adapté.
- `window.generateJson` exposé globalement alors qu’un listener suffit.
- Pas de modes strict error UI communes (`fetchJson` helper avec CSRF + parse + toast).
- Timers BDC non annulés si double-clic rapide avant `disabled` (fenêtre court).

#### P2 — HTML sémantique inégal

- BDC enveloppe le contenu dans `<main>` — bon.
- Reporting / réconciliation : pas de `<main>` explicite (seulement `.main-content` dans le layout).
- Headers de groupe parfois en `<div>` plutôt qu’en titres hiérarchisés cohérents dans les exports.

---

### 5.6 Responsive

| Zone | Desktop | ≤768px | Commentaire |
|---|---|---|---|
| Topbar links | OK | **Masqués** | P0 : navigation morte |
| Formulaires grid | `auto-fit` | OK | Bien conçu |
| Cards reporting | multi-colonnes | OK | `minmax(260px)` |
| Tables réconciliation | OK | scroll horizontal probable | Pas de wrapper `overflow-x: auto` |
| Export `report.html` `.grid` | 2 cols fixes | pas de breakpoint 1 col écran | Print OK |
| Dashboard | grid OK | hero/padding adaptés | Viewport meta manquant |
| Toasts `min-width: 300px` | OK | peut déborder petits écrans | Ajouter `max-width: calc(100vw - 32px)` |

#### P1 — Tables sans conteneur scrollable

Les tableaux d’écarts n’ont pas de wrapper responsive. Sur mobile, colonnes Facturé / Réalisé / Δ risquent de casser la mise en page ou forcer un zoom.

#### P2 — Topbar hauteur fixe + padding body

`padding-top: 70px` global y compris pages sans topbar (auth). Fine correction CSS contextuelle souhaitable.

## 6. Matrice de criticité (synthèse)

| ID | Domaine | Constat | Priorité |
|---|---|---|---|
| F-01 | Responsive / a11y | Nav topbar cachée sans alternative mobile | **P0** |
| F-02 | UX | Texte BDC « saisir un nom de fichier » obsolète | **P0** |
| F-03 | a11y | Toasts sans live region | P1 |
| F-04 | a11y | Progress bar non sémantique + motion non réduite | P1 |
| F-05 | Perf / sécu | Inline CSS → CSP `unsafe-inline` + dette | P1 |
| F-06 | Perf | Google Fonts / `@import` dashboard | P1 |
| F-07 | Sécu UI | Token d’analyse affiché dans export HTML | P1 |
| F-08 | UX | Feedback dashboard start/stop silencieux | P1 |
| F-09 | UX | Progression BDC factice | P1 |
| F-10 | Responsive | Tables sans overflow ; exports / dashboard viewport | P1 |
| F-11 | Archi | URLs `127.0.0.1:port` hardcodées | P1 |
| F-12 | CSS | Duplication thèmes export/dashboard | P2 |
| F-13 | a11y | Landmarks, focus-visible boutons, captions tables | P2 |
| F-14 | Perf | Polling 4 s ; backdrop-filter ; pas de cache static | P2 |
| F-15 | HTML | Meta `selected-month` hors `<head>` | P2 |

## 7. Plan de remédiation recommandé

### P0 — Corrections immédiates

1. **Navigation mobile** : menu burger / drawer / liste accessible ; ne jamais `display:none` sans alternative.
2. **Copy BDC** : aligner le texte sur le comportement réel (« le dossier `pdfs/` est scanné ; un JSON `convert_bdc_MM-YYYY.json` est produit »).

### P1 — Qualité & durcissement UI

1. Extraire les layouts récurrents (stats, steps, pills, form-grid, alert stacks) vers `global.css` ; purger les `style=""` des templates.
2. Self-host des polices ; supprimer `@import` Google du dashboard.
3. Renforcer a11y toasts (`aria-live`), progress (`role="progressbar"`), accordéon (`aria-controls`), `:focus-visible`, `prefers-reduced-motion`.
4. Retirer le token de la page `export.html` (garder seulement l’usage URL côté app live).
5. Brancher `App.showToast` sur le dashboard ; vérifier `response.ok`.
6. Remplacer la barre BDC cosmétique par état indéterminé ou suivi réel.
7. Wrapper tables (`overflow-x:auto`) ; ajouter viewport meta dashboard + export.
8. Paramétrer les URLs de navigation (config/`data-*`/variables d’env) plutôt que hardcoder loopback.

### P2 — Consolidation

1. Factoriser CSS print des exports dans une feuille partagée (`static/css/print-report.css`).
2. Extraire le template dashboard dans un fichier Jinja (plus de megastring Python).
3. Helper JS `App.fetchJson(url, options)` (CSRF, parse, erreurs toast).
4. Cache-Control hashed ou version query sur `/static`.
5. Tests smoke UI légers (Playwright) : nav mobile visible, CSRF header présent, conversion affiche toast, export sans token visible.
6. Lighthouse / axe-core en CI sur les 4 surfaces principales.

## 8. Critères d’acceptation suggérés

- [ ] Sur 375px de large, les 3 apps restent joignables via la topbar (ou équivalent).
- [ ] Aucune mention d’un champ « nom de fichier » côté BDC.
- [ ] CSP documentée ; plan pour retirer `'unsafe-inline'` des styles (classes uniquement).
- [ ] Toasts annoncés (`aria-live`) ; boutons focusables visibles au clavier.
- [ ] `prefers-reduced-motion: reduce` coupe les animations non essentielles.
- [ ] Export rapprochement n’affiche plus le token.
- [ ] Dashboard notifie succès/échec start-stop.
- [ ] Polices servies depuis `/static` (ou fallback système documenté).

## 9. Conclusion

Le frontend remplit son rôle d’outil interne : thème unifié, interactions simples, CSRF client correctement câblé, peu de surface XSS DOM. Les écarts majeurs ne sont pas « cosmétique » : **navigation mobile absente**, **texte métier faux sur le BDC**, et une **organisation CSS inline** qui empêche à la fois la maintenabilité et une CSP stricte.

Prioriser P0 puis le chantier d’extraction CSS / a11y / feedback dashboard. Les exports print sont déjà solides et méritent surtout d’être factorisés, pas réinventés.

---

*Audit réalisé par revue de code statique le 2026-07-16. Une passe instrumentée (Lighthouse, axe, clavier réel, lecteur d’écran) est recommandée après remédiation P0/P1.*
