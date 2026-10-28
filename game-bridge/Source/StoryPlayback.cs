using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

// Current native Celeste 1.4.0.0 / Everest story routines, audited at their real
// input boundaries. This registry deliberately does not include Mod subclasses,
// ambient NPC loops or gameplay tutorials. Native upgrades require regression.
internal static class StoryPlayback {
    private const BindingFlags Fields = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    private static readonly Dictionary<Type, MethodInfo> Methods = new();
    private static readonly HashSet<Type> StoryOwners = new();

    static StoryPlayback() {
        Register(typeof(CS00_Granny), true, "Cutscene", "Meet", "RunAlong", "LaughAndAirQuotes", "Laugh", "StopLaughing", "OminousZoom", "PanToMaddy");
        Register(typeof(CS00_Ending), true, "Cutscene", "WaitFor");
        Register(typeof(CS01_Ending), true, "Cutscene", "EndCityTrigger");
        Register(typeof(CS02_Ending), true, "Cutscene");
        Register(typeof(CS02_BadelineIntro), true, "Cutscene", "TurnAround", "RevealBadeline", "StartLaughing", "StopLaughing");
        Register(typeof(CS02_Mirror), true, "Cutscene", "ZoomBack");
        Register(typeof(CS02_DreamingPhonecall), true, "Cutscene", "ShowShadowMadeline", "WireFalls");
        Register(typeof(CS02_Journal), true, "Routine");
        Register(typeof(CS03_Diary), true, "Routine");
        Register(typeof(CS03_Guestbook), true, "Routine");
        Register(typeof(CS03_Memo), true, "Routine");
        Register(typeof(CS03_Ending), true, "Cutscene", "OshiroTurns", "MoveGhostTo", "GhostSmash", "GhostSmash", "RunPlayerRight");
        Register(typeof(CS03_OshiroBreakdown), true, "Cutscene", "PanCamera", "WalkLeft", "WalkRight", "CreateDustA", "CreateDustB", "MoveDust");
        Register(typeof(CS03_OshiroClutter), true, "Cutscene", "Collapse");
        Register(typeof(CS03_OshiroRooftop), true, "Cutscene", "MaddyWalkAway", "MaddyTurnAround", "EnterOshiro", "OshiroGetsAngry", "OshiroTransform", "AnxietyAndCameraOut");
        Register(typeof(CS03_OshiroMasterSuite), true, "Cutscene", "Wander", "AwkwardPause", "SuiteShadowAppear", "SuiteShadowDisrupt", "Collapse", "Console", "JumpBack", "SuiteShadowCeiling");
        Register(typeof(CS03_OshiroHallway2), true, "Cutscene");
        Register(typeof(CS03_OshiroHallway1), true, "Cutscene");
        Register(typeof(CS03_OshiroLobby), true, "Cutscene", "ZoomOut");
        Register(typeof(CS03_TheoEscape), true, "Cutscene", "StartRemoveVent", "StopRemovingVent", "RemoveVent", "GivePhone");
        Register(typeof(CS04_Gondola), true, "Cutscene", "EnterTheo", "CheckOnTheo", "GetUpTheo", "LookAtLever", "PullLever", "WaitABit", "WaitForCenter", "SelfieThenStallsOut", "MovePlayerLeft", "SnapLeverOff", "DarknessAppears", "DarknessConsumes", "CantBreath", "StartBreathing", "Ascend", "TheoTakesOutPhone", "FaceTheo", "ShowPhoto", "PanCamera", "MovePlayerOnGondola", "MoveTheoOnGondola");
        Register(typeof(CS04_Granny), true, "Cutscene", "Laughs", "StopLaughing", "WaitABeat", "ZoomIn", "MaddyTurnsAround", "MaddyApproaches", "MaddyWalksPastGranny");
        Register(typeof(CS04_MirrorPortal), true, "Cutscene", "CenterCamera", "MusicFadeOutBSide");
        Register(typeof(CS05_Badeline), true, "Cutscene", "BadelineLeaves");
        Register(typeof(CS05_Entrance), true, "Cutscene", "MaddyTurnsRight", "TheoTurns", "TheoLeaves");
        Register(typeof(CS05_Reflection1), true, "Cutscene", "MadelineFallsToKnees", "MadelineStopsPanicking", "MadelineGetsUp");
        Register(typeof(CS05_SaveTheo), true, "Cutscene", "TryToBreakCrystal");
        Register(typeof(CS05_SeeTheo), true, "Cutscene", "ZoomIn", "MadelineTurnsAround", "WaitABit", "MadelineTurnsBackAndBrighten", "Brighten");
        Register(typeof(CS05_TheoInMirror), true, "Cutscene");
        Register(typeof(CS05_TheoPhone), true, "Routine", "WalkToPhone", "StandBackUp");
        Register(typeof(CS06_Ending), true, "Cutscene", "GrannyEnter", "TheoEnter", "MaddyTurnsRight", "BadelineTurnsRight", "BadelineTurnsLeft", "WaitAbit", "TurnToLeft", "TheoRaiseFist", "TheoStopTired");
        Register(typeof(CS06_Campfire), true, "PlayerLightApproach", "Cutscene", "WaitABit", "SelfieSequence", "BeerSequence");
        Register(typeof(CS06_BossIntro), true, "Cutscene", "BadelineFloat", "PlayerStepForward");
        Register(typeof(CS06_BossMid), true, "Cutscene");
        Register(typeof(CS06_BossEnd), true, "Cutscene", "StartMusic", "PlayerHug", "BadelineCalmDown", "CenterCameraOnPlayer", "PictureFade", "WaitForPress");
        Register(typeof(CS06_Granny), true, "Cutscene", "ZoomIn", "Laughs", "StopLaughing", "MaddyWalksLeft", "MaddyWalksRight", "WaitABit", "MaddyTurnsRight");
        Register(typeof(CS06_Reflection), true, "Cutscene");
        Register(typeof(CS06_StarJumpEnd), true, "Cutscene", "TentaclesAppear", "TentaclesGrab", "StartCirclingPlayer", "EndCirclingPlayer", "BadelineCirclePlayer", "FeatherMinigame", "EndFeatherMinigame", "BadelineFlyDown", "NothernLightsDown", "SpinCharacters");
        Register(typeof(CS07_Credits), true, "Routine", "SetupLevel", "WaitForPlayer", "NextLevel", "FadeTo", "BadelineApproachWalking", "DustyRoutine", "BadelineAround", "DoEvent", "EventWaitJumpDash", "EventWaitJumpDoubleDash", "EventClimbDown", "EventWait", "BadelineCombine");
        Register(typeof(CS07_Ending), true, "Cutscene", "WaitABit", "SitDown", "BadelineApproaches");
        Register(typeof(CS07_Ascend), true, "Cutscene", "SpinCharacters");
        Register(typeof(CS08_Ending), true, "Cutscene", "EndingRoutine", "MaddyTurns", "BadelineEmerges", "OshiroEnters", "OshiroSettles", "orig_EndingRoutine");
        Register(typeof(CS08_EnterDoor), true, "Cutscene");
        Register(typeof(CS10_Farewell), true, "Cutscene", "WaitForGranny", "Laugh", "StopLaughing", "StepForward", "GrannyDisappear", "FadeToWhite", "DoFadeToWhite");
        Register(typeof(CS10_FinalLaunch), true, "Cutscene", "WaveCamera", "BirdRoutine");
        Register(typeof(CS10_FinalRoom), true, "Cutscene", "BadelineAppears", "BadelineVanishes");
        Register(typeof(CS10_Ending), true, "Cutscene", "ShowPicture", "EndingRoutine", "Zoom", "Play", "Play", "Loop", "Fade", "orig_EndingRoutine");
        Register(typeof(CS10_MissTheBird), true, "Cutscene", "StandUpFaceLeft", "TakeStepLeft", "TakeStepRight", "FlickerBlackhole", "OpenBlackhole");
        Register(typeof(CS10_FreeBird), true, "Cutscene");
        Register(typeof(CS10_Gravestone), true, "Cutscene", "StepForward", "BadelineAppears", "SitDown", "BirdStuff", "BadelineRejoin");
        Register(typeof(CS10_CatchTheBird), true, "Cutscene", "BirdTwitches", "BirdLooksHurt", "BirdSquakOnGround", "ApproachBird", "ApproachBirdAgain", "BadelineAppears", "WaitABeat", "MadelineSits", "BadelineHugs", "StandUp", "ShiftCameraToBird");
        Register(typeof(CS10_HubIntro), true, "Cutscene");
        Register(typeof(CS10_BadelineHelps), true, "Cutscene", "BadelineAppears", "MadelineFacesAway", "MadelineFacesBadeline", "MadelineStepsForwards", "BadelineVanishes");
        Register(typeof(CS10_MoonIntro), true, "Cutscene", "BadelineTurns", "BadelineAppears", "BadelineVanishes", "FadeIn");
        Register(typeof(CSGEN_StrawberrySeeds), true, "Cutscene");
        Register(typeof(CS00_Ending.EndingCutsceneDelay), true, "Routine");
        Register(typeof(NPC01_Theo), true, "Talk", "Yolo");
        Register(typeof(NPC02_Theo), true, "Talk", "ShowPhotos", "HidePhotos", "Selfie", "SelfieFiltered");
        Register(typeof(NPC04_Granny), true, "TalkRoutine");
        Register(typeof(NPC03_Oshiro_Cluttter), true, "TalkRoutine", "StandUp");
        Register(typeof(NPC03_Oshiro_Suite), true, "Talk");
        Register(typeof(NPC03_Theo_Vents), true, "Talk", "Disappear");
        Register(typeof(NPC07X_Granny_Ending), true, "TalkRoutine", "StartLaughing", "StopLaughing");
        Register(typeof(NPC09_Granny_Inside), true, "TalkRoutine", "StartLaughing", "StopLaughing");
        Register(typeof(NPC09_Granny_Outside), true, "TalkRoutine", "MoveRight", "ExitRight");
        Register(typeof(Player), false, "DummyWalkTo", "DummyWalkToExact", "DummyRunTo", "MoonLanding");
        Register(typeof(Level), false, "ZoomTo", "ZoomAcross", "ZoomBack");
        Register(typeof(NPC), false, "PlayerApproachRightSide", "PlayerApproachLeftSide", "PlayerApproach", "PlayerApproach48px", "PlayerLeave", "MoveTo");
        Register(typeof(NPC03_Oshiro_Cluttter), false, "PaceLeft", "PaceRight");
        Register(typeof(BirdNPC), false, "ShowTutorial", "HideTutorial", "Caw", "StartleAndFlyAway", "FlyAway", "Startle", "FlyTo");
        Register(typeof(Sprite), false, "PlayRoutine", "ReverseRoutine");
        Register(typeof(Textbox), false, "Say");
        Register(typeof(Selfie), false, "PictureRoutine", "FilterRoutine", "OpenRoutine", "WaitForInput", "EndRoutine");
        Register(typeof(BreathingMinigame), false, "Routine", "ShowText", "FadeTextTo", "FadeGameIn", "PopFeather");
        Register(typeof(CS02_Journal.PoemPage), false, "EaseIn", "EaseOut");
        Register(typeof(BadelineDummy), false, "FloatTo", "WalkTo", "SmashBlock");
        Register(typeof(ScreenWipe), false, "Wait");
        Register(typeof(Tween), false, "Wait");
        Register(typeof(CutsceneEntity), false, "CameraTo");
        Register(typeof(FlingBirdIntro), false, "DoGrabbingRoutine");
        Register(typeof(MoonGlitchBackgroundTrigger), false, "GlitchRoutine");
        Register(typeof(DreamBlock), false, "Activate");
        Register(typeof(DreamMirror), false, "BreakRoutine");
        Register(typeof(ResortMirror), false, "SmashRoutine");
        Register(typeof(NPC06_Badeline_Crying), false, "Disperse");
        Register(typeof(ClutterDoor), false, "UnlockRoutine");
        Register(typeof(CS10_HubIntro.Bird), false, "IdleRoutine", "FlyAwayRoutine");
    }

    private static void Register(Type type, bool owner, params string[] names) {
        if (owner) StoryOwners.Add(type);
        foreach (var method in type.GetMethods(Fields | BindingFlags.Static | BindingFlags.DeclaredOnly)) {
            if (names.Length > 0 && Array.IndexOf(names, method.Name) < 0) continue;
            foreach (var attribute in method.GetCustomAttributes<IteratorStateMachineAttribute>()) {
                // Everest can copy the original iterator attribute onto its
                // wrapper as well. Keep the original method authoritative for
                // that iterator, independent of reflection enumeration order.
                if (Methods.TryGetValue(attribute.StateMachineType, out var existing) &&
                    existing.Name.StartsWith("orig_", StringComparison.Ordinal) &&
                    !method.Name.StartsWith("orig_", StringComparison.Ordinal)) continue;
                Methods[attribute.StateMachineType] = method;
            }
        }
    }

    private static object? Owner(IEnumerator iterator) => iterator.GetType().GetField("<>4__this", Fields)?.GetValue(iterator);
    private static int? State(IEnumerator iterator) => StoryText.IteratorState(iterator);
    private static bool IsRoutine(IEnumerator? iterator, Type type, string method) => StoryText.IsRoutine(iterator, type, method);
    private static IEnumerator? Current(Coroutine routine) => Unwrap(StoryText.Current(routine));

    private static IEnumerator? Unwrap(IEnumerator? iterator) {
        if (iterator == null) return null;
        if (Methods.TryGetValue(iterator.GetType(), out var method) && method.Name == "EndingRoutine" &&
            (method.DeclaringType == typeof(CS08_Ending) || method.DeclaringType == typeof(CS10_Ending))) {
            foreach (var field in iterator.GetType().GetFields(Fields))
                if (field.Name.StartsWith("<orig>", StringComparison.Ordinal)) return field.GetValue(iterator) as IEnumerator ?? (State(iterator) == 0 ? iterator : null);
            return null;
        }
        return iterator;
    }

    private static IEnumerable<IEnumerator> RoutineChain(Coroutine routine) {
        if (!routine.Active || routine.Finished) yield break;
        foreach (var item in routine.enumerators) {
            yield return item;
            if (item is Celeste.Mod.SwapImmediatelyExtension.Flattened flat)
                foreach (var nested in flat.enums) yield return nested;
        }
    }

    private static bool Driver(MethodInfo method) => method.Name is "Cutscene" or "Routine" or "EndingRoutine" or "orig_EndingRoutine" or "Talk" or "TalkRoutine";

    private static bool HasLiveDriver(Level level, Entity entity) {
        if (!entity.Active || !StoryOwners.Contains(entity.GetType()) || !level.Entities.entities.Contains(entity) ||
            level.Entities.toRemove.Contains(entity) || entity.Components == null) return false;
        foreach (var component in entity.Components) {
            if (component is not Coroutine routine) continue;
            foreach (var ancestor in RoutineChain(routine))
                if (ReferenceEquals(Owner(ancestor), entity) && Methods.TryGetValue(ancestor.GetType(), out var method) &&
                    StoryOwners.Contains(method.DeclaringType!) && Driver(method)) return true;
        }
        return false;
    }

    internal static bool CompletedDialogueChild(Level level, Textbox box, IEnumerator parent) {
        if (State(parent) is not (1 or 2 or 3 or 5 or 6 or 9 or 10 or 11 or 15)) return false;
        if (StoryText.CompletedChild(parent, box, typeof(Textbox), "EaseOpen", "EaseClose")) return true;
        if (State(parent) != 10 || !box.isInTrigger || parent.Current is not IEnumerator child || !StoryText.IteratorFinished(child) ||
            !Methods.ContainsKey(child.GetType()) || Owner(child) is not Entity owner) return false;
        return HasLiveDriver(level, owner);
    }

    private static bool HasStoryOwner(Level level, Coroutine routine, bool callback = false) {
        foreach (var iterator in RoutineChain(routine)) {
            if (Owner(iterator) is not Entity entity || !entity.Active || !StoryOwners.Contains(entity.GetType()) ||
                !level.Entities.entities.Contains(entity) || level.Entities.toRemove.Contains(entity)) continue;
            if (Methods.TryGetValue(iterator.GetType(), out var method) && StoryOwners.Contains(method.DeclaringType!) && Driver(method)) return true;
            // Textbox invokes NPC.PlayerApproach48px on its own coroutine. Bind
            // that inherited callback back to the NPC's live audited talk root.
            if (callback && HasLiveDriver(level, entity)) return true;
        }
        return false;
    }

    // These resume-state sets come from the installed native iterator IL, not
    // guessed scene flags. Unknown states and replaced iterators fail closed.
    private static string Phase(IEnumerator? iterator) {
        if (iterator == null || !Methods.TryGetValue(iterator.GetType(), out var method)) return "unknown";
        int? state = State(iterator);
        Type? owner = method.DeclaringType;
        string name = method.Name;
        if (owner == typeof(CS00_Ending) && name == "Cutscene")
            return state == 7 ? "tutorial" : state is 0 or 1 or 2 or 3 or 4 or 5 or 6 or 8 or 9 or 10 or 11 or 12 ? "auto" : "unknown";
        if (owner == typeof(CS02_Journal) && name == "Routine")
            return state == 4 ? "confirm" : state is 0 or 1 or 2 or 3 or 5 ? "auto" : "unknown";
        if (owner == typeof(CS06_BossEnd) && name == "WaitForPress")
            return state == 1 ? "confirm" : state == 0 ? "auto" : "unknown";
        if (owner == typeof(CS06_Campfire) && name == "Cutscene")
            return state == 16 ? "choice" : state is >= 0 and <= 19 ? "auto" : "unknown";
        if (owner == typeof(CS08_Ending) && name == "orig_EndingRoutine")
            return state == 4 ? "confirm" : state is 0 or 1 or 2 or 3 or 5 ? "auto" : "unknown";
        if (owner == typeof(CS10_Ending) && name == "orig_EndingRoutine")
            return state == 2 ? "confirm" : state is 0 or 1 or 3 or 4 or 5 ? "auto" : "unknown";
        if ((owner == typeof(CS08_Ending) || owner == typeof(CS10_Ending)) && name == "EndingRoutine")
            return state == 0 ? "auto" : "unknown";
        if (owner == typeof(Selfie) && name == "WaitForInput")
            return state == 1 && Owner(iterator) is Selfie { waitForKeyPress: true } ? "confirm" : state == 0 ? "auto" : "unknown";
        if (owner == typeof(BreathingMinigame) && name == "ShowText")
            return state == 4 ? "confirm" : state is 0 or 1 or 2 or 3 or 5 ? "auto" : "unknown";
        if (owner == typeof(BreathingMinigame) && name == "Routine")
            return state == 9 ? "breathing" : state is >= 0 and <= 16 ? "auto" : "unknown";
        return "auto";
    }

    internal static bool Automatic(Level level, Coroutine routine, bool callback = false) => HasStoryOwner(level, routine, callback) && Phase(Current(routine)) == "auto";

    private static IEnumerable<Coroutine> SceneRoutines(Level level) {
        foreach (var entity in level.Entities) {
            if (!entity.Active || level.Entities.toRemove.Contains(entity)) continue;
            Coroutine? dialogue = entity is Textbox box ? box.runRoutine : null;
            if (dialogue != null) yield return dialogue;
            if (entity.Components == null) continue;
            foreach (var component in entity.Components)
                if (component is Coroutine routine && !ReferenceEquals(routine, dialogue)) yield return routine;
        }
    }

    internal static string? InputNode(Level level) {
        foreach (var routine in SceneRoutines(level)) {
            var iterator = Current(routine);
            bool minigame = iterator != null && Owner(iterator) is BreathingMinigame game && level.Entities.entities.Contains(game);
            if (!minigame && !HasStoryOwner(level, routine, true)) continue;
            string phase = Phase(iterator);
            if (phase is "confirm" or "breathing") return phase;
        }
        return null;
    }

    internal static BirdTutorialGui? SceneTutorial(Level level) {
        var ending = StoryText.SceneEntity<CS00_Ending>(level);
        if (ending == null) return StoryText.SceneEntity<BirdTutorialGui>(level);
        var tutorial = ending.bird?.gui;
        return tutorial != null && level.Entities.entities.Contains(tutorial) && !level.Entities.toRemove.Contains(tutorial) ? tutorial : null;
    }

    internal static bool TutorialReady(Level level, BirdTutorialGui tutorial) {
        var ending = StoryText.SceneEntity<CS00_Ending>(level);
        if (ending == null) return true; // preserve ordinary playable bird hints
        if (!ReferenceEquals(ending.bird?.gui, tutorial) || ending.Components == null) return false;
        foreach (var component in ending.Components) {
            if (component is not Coroutine routine) continue;
            var iterator = Current(routine);
            if (iterator != null && ReferenceEquals(Owner(iterator), ending) && Phase(iterator) == "tutorial") return true;
        }
        return false;
    }

    internal static (string State, string Source)? Presentation(Level level) {
        if (StoryText.SceneEntity<BreathingMinigame>(level) is { Active: true } breathing) {
            foreach (var component in breathing.Components) {
                if (component is not Coroutine routine) continue;
                foreach (var ancestor in RoutineChain(routine))
                    if (IsRoutine(ancestor, typeof(BreathingMinigame), "Routine") && ReferenceEquals(Owner(ancestor), breathing))
                        return Phase(Current(routine)) == "auto" ? ("playing", "breathing_animation") : ("unknown", "breathing_unrecognized_routine");
            }
            return ("unknown", "breathing_unrecognized_routine");
        }
        if (StoryText.SceneEntity<Selfie>(level) is { Visible: true } selfie) {
            foreach (var routine in SceneRoutines(level)) foreach (var ancestor in RoutineChain(routine))
                if (ReferenceEquals(Owner(ancestor), selfie))
                    return Phase(Current(routine)) == "auto" && HasStoryOwner(level, routine, true)
                        ? ("playing", "selfie_animation") : ("unknown", "selfie_unrecognized_routine");
            return ("unknown", "selfie_unrecognized_routine");
        }
        return null;
    }

    internal static bool HasVisibleStoryHud(Scene? scene) {
        if (scene is IntroVignette intro) return intro.textAlpha > 0f || intro.menu != null;
        if (scene is not Level level) return StoryText.ScenePostcard(scene) is { Visible: true, alpha: > 0f };
        if (StoryText.RenderedEntity<Textbox>(level) is { Visible: true, ease: > 0f } ||
            StoryText.RenderedEntity<CS03_Memo.MemoPage>(level) is { Visible: true, alpha: > 0f } ||
            StoryText.RenderedEntity<CS02_Journal.PoemPage>(level) is { Visible: true, alpha: > 0f } ||
            StoryText.RenderedEntity<Selfie>(level) is { Visible: true, image: { Visible: true } } ||
            StoryText.RenderedEntity<BreathingMinigame>(level) is { Visible: true, bgAlpha: > 0f } ||
            StoryText.RenderedEntity<BirdTutorialGui>(level) is { Visible: true, Scale: > 0f } ||
            StoryText.RenderedEntity<PrologueEndingText>(level) is { Visible: true } ||
            StoryText.RenderedEntity<MemorialText>(level) is { Visible: true, alpha: > 0f } ||
            StoryText.RenderedEntity<Poem>(level) is { Visible: true, Alpha: > 0f } ||
            StoryText.RenderedEntity<CS06_BossEnd>(level) is { Visible: true, pictureFade: > 0f } ||
            StoryText.RenderedEntity<CS08_Ending>(level) is { Visible: true } ||
            StoryText.RenderedEntity<CS10_Ending>(level) is { Visible: true }) return true;
        return false;
    }

    internal static bool WriteUi(Utf8JsonWriter writer, Level level) {
        string? input = InputNode(level);
        if (StoryText.SceneEntity<BreathingMinigame>(level) is { Visible: true } game) {
            writer.WriteStartObject("ui");writer.WriteString("type", "BreathingMinigame");
            writer.WriteBoolean("inputReady", input is "breathing" or "confirm");
            if (game.textAlpha > 0f) writer.WriteString("text", game.text);
            if (game.featherAlpha > 0f) writer.WriteNumber("feather", game.feather);
            if (game.boxAlpha > 0f) {
                writer.WriteNumber("targetCenter", game.currentTargetCenter);
                writer.WriteNumber("targetHalfHeight", game.currentTargetBounds);
            }
            writer.WriteString("controls", input == "confirm" ? "C: confirm" : "Hold J/X/U to raise feather; release to lower");
            writer.WriteEndObject();return true;
        }
        string? type = StoryText.SceneEntity<CS02_Journal.PoemPage>(level) is { Visible: true, alpha: > 0f } ? "JournalPoem" :
            StoryText.SceneEntity<Selfie>(level) is { Visible: true, image: { Visible: true } } ? "Selfie" :
            StoryText.SceneEntity<CS06_BossEnd>(level) is { Visible: true, pictureFade: > 0f } ? "BossPicture" :
            StoryText.SceneEntity<CS08_Ending>(level) is { Visible: true } ? "EpilogueEnding" :
            StoryText.SceneEntity<CS10_Ending>(level) is { Visible: true } ? "FarewellEnding" : null;
        if (type == null) return false;
        writer.WriteStartObject("ui");writer.WriteString("type",type);
        writer.WriteBoolean("inputReady",input == "confirm");writer.WriteString("controls","C: confirm when inputReady");
        if (type == "JournalPoem" && StoryText.SceneEntity<CS02_Journal.PoemPage>(level) is { } poem)
            writer.WriteString("text", StoryText.ReadPage(poem.text, 0, poem.alpha));
        writer.WriteEndObject();return true;
    }
    internal static bool PlayerIntro(Player player) {
        if (player.Dead || player.StateMachine is not { } machine) return false;
        var current = Current(machine.currentCoroutine);
        string? method = machine.State switch {
            Player.StIntroWalk => "IntroWalkCoroutine",
            Player.StIntroJump => "IntroJumpCoroutine",
            Player.StIntroMoonJump => "IntroMoonJumpCoroutine",
            Player.StIntroWakeUp => "IntroWakeUpCoroutine",
            Player.StIntroThinkForABit => "IntroThinkForABitCoroutine",
            _ => null,
        };
        if (method == null) return false;
        bool nativeRoot = false;
        foreach (var ancestor in RoutineChain(machine.currentCoroutine))
            if (IsRoutine(ancestor, typeof(Player), method) && ReferenceEquals(Owner(ancestor), player)) nativeRoot = true;
        return nativeRoot && (IsRoutine(current, typeof(Player), method) ||
            (machine.State == Player.StIntroMoonJump && IsRoutine(current, typeof(Player), "MoonLanding")) ||
            (machine.State == Player.StIntroWakeUp && IsRoutine(current, typeof(Sprite), "PlayRoutine")));
    }

}
