# Facturation — guide d’utilisation

Ce guide décrit le fonctionnement de l’acte 4 et les scénarios de régularisation.
Le service prépare une **base de facturation** : il ne produit pas la facture légale.
Le PDF officiel reste émis dans Indy ; le service versionne, contrôle, exporte et
archive les éléments de préparation.

## 1. Démarrage

```bash
./scripts/launch_services.sh     # dashboard : http://127.0.0.1:5500/
# ou uniquement la facturation
make serve-billing               # http://127.0.0.1:5040/
```

Ouvrir ensuite `http://127.0.0.1:5040/?month=YYYY-MM`.

La clé du projet est le **mois de production**, pas le mois d’émission de la facture.
Une production d’octobre peut très bien être facturée en novembre avec un numéro Indy
de novembre.

## 2. Le rituel mensuel en 4 actes

| Acte | Service | Rôle |
|------|---------|------|
| 1 | Convertisseur BDC | Convertir les PDF de bons de commande en JSON |
| 2 | Reporting Vidéos | Scanner la production et générer `rapport-YYYY-MM.json` |
| 3 | Réconciliation | Comparer BDC ↔ production, ajuster, puis **Transmettre à la facturation** |
| 4 | Facturation | Calculer, versionner, contrôler et préparer l’envoi client |

L’acte 4 a besoin :

- du **rapport BDC** `apps/bdc_converter/convert_bdc_MM-YYYY.json` (toujours requis) ;
- du **rapport de production** `apps/reporting/rapport-YYYY-MM.json` ;
- du **snapshot de réconciliation** `data/reconciliation/YYYY-MM.json`, transmis depuis
  l’acte 3 (fortement recommandé).

Si le snapshot est absent, le service recalcule l’analyse avec `compare_reports()` et
affiche un avertissement. Les ajustements de session de l’inspecteur (exclusions,
justifications) sont alors perdus : il vaut mieux refaire le handoff depuis l’acte 3.

## 3. Créer la première version

1. Choisir le mois puis **« Calculer le brouillon »**.
2. Contrôler les lignes et les éventuels points d’attention.
3. **« Geler la version »** avec le statut adapté :
   - **« Envoyée pour vérification »** : proposition à faire valider par le client ;
   - **« Validée client »** : uniquement si le client a déjà validé le contenu.
4. Exporter si besoin :
   - **HTML** : récapitulatif lisible ;
   - **PDF** : document de préparation, mention explicite « ne constitue pas une facture » ;
   - **CSV** : import ou saisie dans Indy.

Tant que la version est un **brouillon**, elle est modifiable. Dès qu’elle est gelée,
elle devient **immuable**.

## 4. Lire les lignes

### Ligne BDC

Une ligne par bon de commande, avec :

- le numéro BDC et la date de livraison ;
- le détail **facturé / commandé / réalisé** par catégorie ;
- le montant calculé par défaut avec :

```text
min(réalisé, commandé) × taux
```

Le mode **« Au commandé »** (dans « Recalculer avec ») facture le total du BDC même si
la production est inférieure.

### Ligne « Lots réalisés sans BDC »

Elle est **automatique**. Elle regroupe :

- les surplus de production sur des BDC couverts ;
- les lots produits sans BDC ;
- le détail par catégorie et par taux.

Elle peut être éditée ou exclue dans un brouillon. Si la version est déjà gelée,
créez une nouvelle version.

### Lignes manuelles

Le bouton **« Ajouter une ligne manuelle »** sert aux frais ou prestations qui ne
ressortent pas des BDC. Une ligne existante modifiée via le formulaire porte le badge
**« Ajustée »** ; les lignes manuelles sont identifiées par leur libellé.

Lors d’un recalcul ou d’une nouvelle version, les **lignes manuelles sont conservées**.
Les lignes automatiques (BDC et provision) sont **recalculées depuis les sources** :
un ajustement sur une ligne automatique n’est pas reporté tel quel, il faut le refaire
si nécessaire. Le diff permet de vérifier ce qui a changé.

## 5. Statuts et transitions

| Statut | Sens | Modifiable ? |
|--------|------|--------------|
| `brouillon` | Travail en cours | Oui |
| `envoyé_pour_vérification` | Version gelée, envoyée au client | Non |
| `validé_client` | Contenu accepté par le client | Non |
| `émis` | Facture légale émise dans Indy | Non |
| `remplacé` | Version remplacée par une version plus récente | Non |
| `annulé` | Version abandonnée avant émission | Non |

Transitions possibles :

```text
brouillon → envoyé_pour_vérification → validé_client → émis → remplacé
                     ↘ annulé                 ↘ annulé
```

Le passage à **`émis`** exige :

- la certification de réconciliation, ou un **override justifié** ;
- le **numéro Indy** et la **date d’émission** ;
- un total Indy identique au total de la version, ou une **note d’écart**.

Le service n’attribue pas les numéros de facture : vous saisissez le numéro Indy
(ex. `KD202611-45`).

## 6. Scénarios

### A. Mois conforme, sans écart

1. Actes 1 à 3, puis handoff.
2. Acte 4 : calculer le brouillon.
3. Geler directement en **« Validée client »** (ou passer par « Envoyée pour
   vérification » si le client doit confirmer).
4. Si vous utilisez le suivi Indy : renseigner numéro/date, attacher le PDF, puis
   **« Émise »**.

C’est le cas d’octobre 2026 : 7 lignes BDC, 1 676,00 €, certification 100 %.

### B. Mois avec écarts, le client accepte la provision

1. Calculer le brouillon : les écarts sont dans la ligne automatique
   « Lots réalisés sans BDC ».
2. Geler en **« Envoyée pour vérification »**.
3. Le client accepte la provision.
4. Passer en **« Validée client »**, puis éventuellement en **« Émise »**.

Aucune nouvelle version n’est nécessaire.

### C. Mois avec écarts, le client envoie des BDC corrigés

C’est le scénario principal de régularisation.

1. **Acte 1** : convertir les BDC corrigés ou nouvellement reçus.
2. **Acte 2** : à refaire **uniquement si la production vidéo a changé**. Une simple
   correction de BDC ne nécessite pas de rescanner les archives.
3. **Acte 3** : rouvrir la réconciliation, vérifier les écarts, puis
   **« Transmettre à la facturation »** pour créer un nouveau snapshot.
4. **Acte 4** : ouvrir la v1 gelée, puis **« Nouvelle version »**.
   - La v2 est recalculée depuis les sources.
   - Les lignes BDC sont mises à jour.
   - Les lignes manuelles sont conservées ; vérifiez qu’elles restent pertinentes.
   - La provision diminue ou disparaît.
   - Le diff **« Évolution depuis la version 1 »** s’affiche automatiquement.
5. Passer la v2 en **« Envoyée pour vérification »**, puis **« Validée client »**
   après accord.
6. Traiter la v1 :
   - **« Annulée »** si elle n’a jamais été émise ;
   - **« Remplacée »** si elle avait déjà été émise (l’avoir éventuel se gère dans Indy).

> Ne modifiez pas la v1 pour la faire devenir « validée » après coup. La v1 reste
> l’historique de ce qui a été envoyé ; la v2 devient la version de référence.

### D. Lots déjà facturés ailleurs, doublons ou chevauchements

1. Dans l’acte 3, utiliser l’inspecteur pour **exclure** un jingle/doublon ou
   **justifier** un écart.
2. Refaire **« Transmettre à la facturation »**.
3. Dans l’acte 4, créer une **nouvelle version** depuis la version gelée.

Les lots déjà facturés sur un mois voisin sont automatiquement exclus de la provision.

### E. Un taux manque pour une catégorie

Le service bloque la finalisation avec un message du type
« Aucun taux connu pour la catégorie… ». C’est volontaire : il refuse de facturer à un
taux inventé.

Solution :

1. Créer ou convertir un BDC contenant cette catégorie, puis relancer les actes 1 et 3 ;
2. ou, dans le brouillon, ouvrir **« Recalculer avec »**, renseigner le taux manquant
   dans **« Taux unitaires (€ HT) »**, puis **« Recalculer depuis les sources »**.

Les taux détectés apparaissent en placeholder. Laisser vide pour utiliser le taux déduit
des BDC.

### F. La facture légale a déjà été émise, puis le client corrige

Le service **ne gère pas les avoirs**. La correction comptable se fait dans Indy.

1. Émettre l’avoir dans Indy.
2. Refaire les actes 1 et 3 avec les BDC corrigés, puis le handoff.
3. Créer une **v2** dans l’acte 4.
4. Marquer la v1 **« Remplacée »** une fois la situation régularisée.
5. Renseigner la nouvelle facture Indy sur la v2 et l’attacher.

## 7. Check-list mensuelle

### Avant le handoff

- [ ] Les BDC du mois sont convertis (acte 1).
- [ ] Le rapport de production du mois est généré (acte 2).
- [ ] La réconciliation est ouverte avec le bon `?month=YYYY-MM`.
- [ ] Les écarts sont inspectés, exclus ou justifiés.
- [ ] Le mois est certifié, ou les écarts restants sont assumés.

### Après le handoff

- [ ] Le brouillon est calculé dans l’acte 4.
- [ ] Les lignes BDC et la provision sont cohérentes.
- [ ] Les avertissements sont lus.
- [ ] La version est gelée avec le bon statut.
- [ ] Les exports HTML/PDF/CSV sont générés si nécessaire.

### Après régularisation

- [ ] Les BDC corrigés sont convertis.
- [ ] La réconciliation a été refaite et transmise à nouveau.
- [ ] Une **nouvelle version** a été créée (pas de modification de la version gelée).
- [ ] Le diff est vérifié.
- [ ] L’ancienne version est annulée ou remplacée.
- [ ] Le numéro et la date Indy sont renseignés sur la version finale.

## 8. Messages fréquents

| Message | Que faire |
|---------|-----------|
| « Aucun snapshot de réconciliation » | Faire le handoff depuis l’acte 3. Le calcul reste possible mais sans les ajustements de session. |
| « Sources modifiées depuis le gel de cette version » | C’est normal : la version gelée reste immuable. Refaire les actes concernés, puis créer une nouvelle version. |
| « Le snapshot de réconciliation ne correspond plus au rapport… » | Les sources ont changé depuis le handoff. Refaire l’acte 3 et transmettre à nouveau. |
| « Aucun taux connu pour la catégorie… » | Ajouter un BDC avec cette catégorie ou renseigner le taux dans « Recalculer avec ». |
| « Transition impossible depuis… » | Le statut cible n’est pas accessible depuis le statut actuel. Utiliser le bouton prévu pour le prochain statut. |
| « La version doit être validée client avant… » | Passer d’abord par « Validée client ». |
| « Le numéro de facture Indy est obligatoire… » | Renseigner le numéro et la date dans le bloc « Facture Indy ». |

## 9. Où sont les fichiers ?

```text
data/
  reconciliation/YYYY-MM.json   # snapshot transmis par l’acte 3
  billing/YYYY-MM/
    project.json                # index des versions
    versions/vN.json            # version gelée ou brouillon
    versions/vN-facture.pdf     # PDF Indy attaché
    sources/vN/                 # copies des sources au moment du calcul
```

Chaque version archive les empreintes SHA-256 de ses sources. Une version gelée
n’est jamais recalculée : ses sources et ses montants restent ceux du moment du gel.

## 10. Ce que le service ne fait pas

- Il ne produit pas la facture légale : le PDF officiel vient d’Indy.
- Il ne tient pas de grand livre ni de balance comptable.
- Il ne gère pas les avoirs.
- Il n’attribue pas les numéros de facture.
- Il ne remplace pas la validation client : les statuts servent à tracer cette validation.
