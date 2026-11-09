# Passivt fältprov: slutkontroll 2026-11-09

Granskningsbas: `c76568ceff2053bdef84cc57dde837a879faa41a`.
Produktionsreferens: `5100ed83d1ff7ac643754a65460d8aa63e1265c5`.
Arbetsgren: `experiment/thermal-evidence-chain-20261109`.
Ingen produktion har lästs eller ändrats under denna slutkontroll.

## Beslut och säker provkonfiguration

GO för installation av ett tidsbegränsat, strikt passivt insamlingsprov, efter separat installationsgodkännande. NO-GO för adaptiv styrning och för besparings-/prognosnyttopåståenden.

Sätt följande via befintliga inställningar, med riktiga booleska värden:

```yaml
thermal_intelligence_enabled: true
adaptive_braking_enabled: false
```

Övriga redan godkända inställningar för PI, priser, förvärmning, komfort, SAFE, Ohmigo, GOS och Relay Guard ska bevaras. Inga nya sensorer krävs. Detta dokument eller koden ändrar inga användarinställningar automatiskt.

Starta provet med en färsk sensorinstans efter den planerade installationen/omstarten. Kontrollera därefter i verkliga diagnostikattribut:

- `adaptive_braking.multiplier == 1.0`, state `base` och reason `disabled`.
- `thermal_intelligence.passive_field_trial_safe == true`.
- `legacy_adaptive_enabled == false` och `legacy_adaptive_return_pending == false`.
- `active_control == false` och `fit_trusted == false`.

Att enbart slå av den gamla adaptiva flaggan under pågående drift räcker inte som bevis för omedelbar Base: godkänd produktionslogik kan fortfarande rampa tillbaka en tidigare korrigering. Den övergående historiken lagras inte över en ny sensorinstans. Vi har bevarat återgångsbeteendet och gjort säkerhetsmärkningen ärlig om kvarvarande historik. Om flaggorna är båda på kan den gamla adaptionen fortfarande styra efter sin egen kvalificering; det är uttryckligen ingen passiv provkonfiguration.

## Konkreta rättningar

1. Fel i RTM:s `summary()` och ForecastAudits `fixed_summary()` kunde inträffa i `_base_attrs` före fysisk utgång. De är nu isolerade; diagnostiken anger otillgänglighet och Base fortsätter.
2. Fel i `fit_due`, snapshotförberedelse eller HA:s taskschemaläggning kunde efter utgången ersätta giltigt kontrolltillstånd med `update_error`. Schemaläggningsfel är nu isolerade och ogiltigförklarar jobbet. En oschemalagd coroutine stängs vid fel.
3. Andra auditändringar kunde kringgå termikens femminutersbatchning och skriva om hela Store-filen varje minut. När termisk evidens finns begränsas nu hela filens ordinarie checkpoints till en per fem minuter. Avlastningsflush tar med senaste audit och termikhistorik även efter en tidigare checkpoint.
4. Enbart den begränsade händelselistan kunde inte visa avvisningsandelen efter flera dagar. Befintligt diagnostikattribut har nu fasta, beständiga räknare med starttid och uttrycklig nämnare. Inga nya entiteter, inställningar eller insamlingssystem infördes.
5. Fältprovets säkerhetsmärkning tar hänsyn till både den äldre adaptiva flaggan och kvarvarande adaptiv ramphistorik.

Alla rättningar följer reproducerade fel eller en konkret eftergranskningsfråga. RTM:s modellprincip, insamlingsregler, styrning och kvalificering har inte byggts om.

## Evidens och diagnostik under provet

Följ `thermal_intelligence` och `thermal_fixed_horizon_audit` på den befintliga styrsensorn:

| Fråga | Befintligt attribut/nytt fält i samma diagnostik |
| --- | --- |
| Kommer data? | `evidence_events`, `latest_evidence_at`, `evidence_diagnostics.counts.source_numeric_changes`, `aggregate_observations`, `command_establishments`, `command_value_changes`, `command_confirmations` |
| Avvisas användbar information? | `source_rejection_fraction`, `source_change_attempts`, `source_changes_rejected` och `reject_report_time_missing/future/stale`, `reject_report_out_of_order` |
| Vad är nämnaren? | `source_rejection_denominator`: valideringsförsök för numeriska ändringar, inklusive kvalitetsomprövningar. `identical_source_rechecks` är separat och kan omfatta pollningar och återpubliceringar; det är inte antal nya fysiska mätningar. |
| Hur långt bak finns rådata? | `oldest_evidence_at`, `latest_evidence_at`, `evidence_events` |
| Klaras omstarter/avbrott? | `restarts`, `interruptions`, `dispatch_failures`, `last_interruption`; jämför räknarna före/efter restart |
| Kan modellen passas? | `model_status`, `fit_attempts`, `fit_results_applied`, `fit_results_stale`, `last_fit_status`, `last_fit_attempt_epoch`, `fit_in_progress`, `excitation_sufficient`, `command_range`, `command_variance` |
| Är provet passivt? | `passive_field_trial_safe`, `legacy_adaptive_enabled`, `legacy_adaptive_return_pending`, `active_control`, samt äldre `adaptive_braking.multiplier` |
| Fungerar lagringen? | `last_checkpoint_requested_at`, `store_read_failed`, Store-fel i HA-loggen, filens storlek/ändringstid |

Räknarna gäller sedan `evidence_diagnostics.started_at`, inte sedan installationens livstidsstart och inte bara det kvarvarande rådatafönstret. En äldre v2-fil utan räknare startar nya räknare på noll; bortkastad historik rekonstrueras inte. Räknarna överlever retention och omstart. Okänd fysisk mättid och värmeleverans förblir okända.

ForecastAudit märker måtten `accepted_numeric_changes_only`. Oförändrade återpubliceringar ger inte konstgjorda träningspunkter eller nya prognosutfall. MAE/skill gäller därför tillgängliga ändringsutfall; flat drift saknar ofta bedömbart utfall. Dessa tal får inte presenteras som representativ prognosförmåga för hela driften.

Följ även befintliga mode/status, temperaturer, komfortgolv, beräknad utgång, observerad Ohmigo-utgång, GOS/Guard-status samt HA-loggens event-loop-/utgångsvarningar och befintliga CPU-/minnesmått om de finns. Kod- och offlineprov bevisar inte faktisk tidsbelastning på produktionsvärden. Jämför med baslinjen och samla avvikande tidpunkter; ändra inte värmepumpskommandon för att skapa variation.

## Belastning och retention

Ingen ny databas: samma HA Store. Högst 2048 råhändelser och 30 dagars maximal ålder, med samma 30-dagarsgräns för den härledda RTM-griden. Två händelser/minut ger ungefär 17 timmars rådata; en/minut cirka 34 timmar och fyra/minut cirka 8,5 timmar. Faktisk täckning ska avläsas, inte antas.

Ordinarie helfilscheckpoint högst var femte minut när termikhistorik finns, följt av befintlig 30-sekundersfördröjning. Normal avlastning gör en extra slutflush. Crash kan förlora det senaste checkpointintervallet; öppna prognoser/kommandofönster återupptas aldrig som känd kontinuitet.

Uppmätt lokal syntetisk belastning efter ändringarna, 2880 cykler: 2048 händelser, export 864565 byte, median insamling 0,052 ms, p95 0,120 ms, max 0,350 ms, export plus JSON-kodning 26,2 ms. En helfil på ungefär denna storlek innebär som mest cirka 249 MB omskriven JSON/dygn vid 288 checkpoints, plus avlastningsflush. Övriga befintliga Store-fält kan göra filen större. Detta är en storleks-/frekvensberäkning, inte uppmätta fysiska flashskrivningar på HA. Kodning och snapshotförberedelse har kvar en begränsad kostnad; modellpassningen körs fortsatt i executor efter fysisk utgång.

Lämpligt första prov är 24–48 timmar. Läs diagnostiken efter start, efter några timmar och minst två gånger per dygn. Bevara begränsade uttag utanför produktionskonfigurationen innan rådata roterar ut om senare oberoende granskning av hela perioden behövs. Efter några dagar finns fortfarande beständiga räknare och modellgrid, men inte nödvändigtvis hela råhistoriken.

## Verifiering och jämförelse mot produktion

Riktade felprov täcker normal PI och faktisk prisbromsning, Ohmigo, GOS och båda utgångarna, samt två på varandra följande kontrolluppdateringar efter fel i insamling, utgångsobservatör, audit, Store, modellförberedelse/schemaläggning och executor. Samtliga flaggkombinationer samt kvalificerad äldre adaptiv modell och oberoende RTM-kandidat testas. Store-flush-felet når faktiskt `async_save`, och ett separat prov verifierar att avlastningshanteraren fångar felet.

Riktade tester: 106 godkända (0,52 s). Full regression: 1621 godkända (44,65 s). Exakt publicerad commit redovisas i leveransen. Ruff F/E9 på ändrad runtime och nya tester samt git diff --check är godkända. Befintlig pytest-varning för asyncio-scope kvarstår.

Mot c76568c ändras endast diagnostik/felisolering, checkpointbeteende och tester. Styrfilerna `control.py`, `ohmigo.py`, `relay_guard.py` och `settings.py` har ingen diff från c76568c. Mot produktionsreferensen finns sedan tidigare observer-hooks i Ohmigo/GOS, den passiva RTM-adaptern, delad audit och den pensionerade separata passiva Pilot-planeraren; detta är alltså ingen byte-identisk produktionsintegration. PI- och Relay Guard-filerna är byte-identiska med produktionsreferensen. SAFE och utgångstjänster testas på riktiga kodvägar med offline-HA-gränser; inga tjänster mot verklig utrustning har anropats.

## Återgång och kvarvarande begränsningar

Före en separat godkänd installation: bevara den installerade integrationskatalogen, relevanta befintliga inställningar och en avgränsad backup av befintlig `pumpsteer.pilot.<entry_id>` Store. Backuperna ska stanna privat och utanför projektets publicerade filer.

Återgång: återställ hela integrationskatalogen från den verifierade produktionsversionen `5100ed83d1ff7ac643754a65460d8aa63e1265c5`, inklusive borttagning av de nytillkomna prototypfilerna. Återställ vid behov just den sparade Store-filen och de tidigare inställningarna, och gör en kontrollerad HA-omstart efter separat godkännande. Återställ inte Recorder-databasen. Kontrollera därefter normal status och utgångar. Ingen sådan åtgärd utförs i detta uppdrag.

Inga påvisade blockerare kvarstår för den angivna passiva provkonfigurationen. Otillräcklig kommandovariation, oidentifierbar värmerespons, givarnas upplösning/rapportluckor, urvalet av prognosutfall och faktisk HA-belastning är kvarvarande fältfrågor. De är inte grund för att sänka kvalitetskrav eller aktivera adaptiv styrning. Avbryt/återgå efter separat beslut vid påverkan på fysisk utgång, återkommande HA-belastningsproblem eller fel i bevarandet av evidens.
