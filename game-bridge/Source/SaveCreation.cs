using System;
using System.IO;
using System.Linq;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>Start a new official save through the game's own new-game callback.</summary>
internal static class SaveCreation {
    public static int Start(string name, bool dryRun = false) {
        name = name.Trim();
        if (name.Length < 1 || name.Length > 20 || name.Any(ch => char.IsControl(ch) || char.IsSurrogate(ch)))
            throw new ArgumentException("name must be 1-20 printable characters");
        if (!Bridge.Active || Engine.Scene is not Overworld overworld || overworld.Current is not OuiFileSelect select)
            throw new InvalidOperationException("create_save requires the file-select screen and an active bridge");
        if (overworld.Next != null) throw new InvalidOperationException("wait for the file-select transition to finish");
        if (UserIO.Saving || select.Slots == null || select.Slots.Any(s => s != null && s.StartingGame))
            throw new InvalidOperationException("a save or new-game operation is already in progress");
        int index = Array.FindIndex(select.Slots, slot => slot != null && slot.FileSlot >= 0 && !slot.Exists && Vacant(slot.FileSlot));
        if (index < 0) throw new InvalidOperationException("no empty save slot is available");
        OuiFileSelectSlot target = select.Slots[index];
        if (target.newGameLevelSetPicker?.NewGameLevelSet is { } levelSet && levelSet != "Celeste")
            throw new InvalidOperationException("create_save currently supports the official Celeste level set");
        if (dryRun) return target.FileSlot;
        // The existing slot object owns the coroutine and Everest hooks. Calling
        // the native action preserves normal initialization and first-area saving.
        select.SlotIndex = index;
        select.SlotSelected = true;
        target.Name = name;
        target.AssistModeEnabled = false;
        target.VariantModeEnabled = false;
        target.OnNewGameSelected();
        return target.FileSlot;
    }

    private static bool Vacant(int slot) {
        string path = UserIO.GetSaveFilePath(SaveData.GetFilename(slot));
        if (File.Exists(path)) return false;
        string? directory = Path.GetDirectoryName(path);
        // An orphaned mod-save belongs to that slot too. Never replace its owner.
        return directory == null || !Directory.Exists(directory) ||
               !Directory.EnumerateFiles(directory, Path.GetFileNameWithoutExtension(path) + "-*.celeste").Any();
    }
}
