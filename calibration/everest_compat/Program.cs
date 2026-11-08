using System.Collections;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using System.Text.Json;
using Celeste.Mod;
using CelesteAI.Bridge;
using Monocle;

// An isolated process with real Everest and production Runtime assemblies.
// No game launch, sockets, files in the installation, or loader execution.
string game = Path.GetFullPath(args[0]);
AssemblyLoadContext.Default.Resolving += (_, name) => {
    string path = Path.Combine(game, name.Name + ".dll");
    return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(path) : null;
};
const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
void Check(bool value, string message) { if (!value) throw new Exception(message); }
void Set(object target, string field, object? value) => target.GetType().GetField(field, flags)!.SetValue(target, value);
object? Invoke(string method, params object?[] values) {
    try { return typeof(BridgeRuntime).GetMethod(method, flags)!.Invoke(null, values); }
    catch (TargetInvocationException ex) { throw ex.InnerException!; }
}
void Reject(Action action, string reason) {
    try { action(); } catch (InvalidOperationException ex) {
        Check(ex.Message.Contains(reason), "Wrong refusal: " + ex); return;
    }
    throw new Exception("Unsafe reload was accepted: " + reason);
}
var modules = (IList)typeof(Everest).GetField("_Modules", flags)!.GetValue(null)!;
var engine = (Engine)RuntimeHelpers.GetUninitializedObject(typeof(Engine));
typeof(Engine).GetProperty("Instance", flags)!.SetValue(null, engine);
typeof(BridgeRuntime).Assembly.GetType("CelesteAI.Bridge.FreezeGate")!
    .GetProperty("Available", flags)!.SetValue(null, true);
EverestModuleAssemblyContext Context(EverestModuleMetadata meta) {
    var context = (EverestModuleAssemblyContext)RuntimeHelpers.GetUninitializedObject(typeof(EverestModuleAssemblyContext));
    Set(context, "ModuleMeta", meta);
    Set(context, "ActiveDependencyContexts", new HashSet<EverestModuleAssemblyContext>());
    return context;
}
HashSet<EverestModuleAssemblyContext> Edges(EverestModuleAssemblyContext context) =>
    (HashSet<EverestModuleAssemblyContext>)context.GetType().GetField("ActiveDependencyContexts", flags)!.GetValue(context)!;
var payload = new EverestModuleMetadata { Name = BridgeRuntime.PayloadName, DLL = "bin/CelesteAIBridge.dll",
    PathDirectory = Path.Combine(Path.GetTempPath(), "celeste-compat-fixture") };
payload.AssemblyContext = Context(payload);
var other = new EverestModuleMetadata { Name = "DependentFixture" };
other.AssemblyContext = Context(other);
modules.Clear();
modules.Add(new FixtureModule { Metadata = payload });
modules.Add(new FixtureModule { Metadata = other });
using var empty = JsonDocument.Parse("{}");
Request request = new(1, "reload_prepare", empty.RootElement);
FieldInfo leaseField = typeof(BridgeRuntime).GetField("lease", flags)!;
int exports = 0;
BridgeRuntime.Attach(Guid.NewGuid(), BridgeRuntime.ContractVersion, "test", _ => { }, _ => { },
    () => true, () => { exports++; return "{}"; }, _ => { });
BridgeRuntime.Active = true;
try {
    Invoke("CheckReverseDependencies", payload); // Unrelated contexts.
    other.AssemblyContext = null;
    Invoke("CheckReverseDependencies", payload); // Non-code Mod.
    other.Dependencies.Add(new EverestModuleMetadata { Name = payload.Name });
    Reject(() => Invoke("Prepare", request, false), "dependent Mod DependentFixture");
    Check(!BridgeRuntime.HoldingReload && leaseField.GetValue(null) == null && exports == 0,
        "Declared dependency refusal created a hold or exported state");
    other.Dependencies.Clear();
    other.AssemblyContext = Context(other);
    Edges(other.AssemblyContext).Add(payload.AssemblyContext);
    Reject(() => Invoke("Prepare", request, false), "dependent Mod DependentFixture");
    Check(!BridgeRuntime.HoldingReload && leaseField.GetValue(null) == null && exports == 0,
        "Actual dependency refusal changed ownership");
    Set(other.AssemblyContext, "ActiveDependencyContexts", null);
    Reject(() => Invoke("Prepare", request, false), "Cannot inspect active dependencies");
    Check(!BridgeRuntime.HoldingReload && leaseField.GetValue(null) == null && exports == 0,
        "Failed query was treated as no dependency");
    Set(other.AssemblyContext, "ActiveDependencyContexts", new HashSet<EverestModuleAssemblyContext>());
    Invoke("Prepare", request, false);
    object lease = leaseField.GetValue(null)!;
    Check(BridgeRuntime.HoldingReload && lease != null && exports == 1, "Safe prepare did not retain its snapshot");
    string id = (string)lease!.GetType().GetField("Id", flags)!.GetValue(lease)!;
    using var commitArgs = JsonDocument.Parse(JsonSerializer.Serialize(new { lease = id }));
    Request commit = new(2, "reload_commit", commitArgs.RootElement);
    void Prepared() {
        Check(ReferenceEquals(lease, leaseField.GetValue(null)) && BridgeRuntime.HoldingReload && exports == 1 &&
            (string)lease.GetType().GetField("Stage", flags)!.GetValue(lease)! == "prepared",
            "Commit refusal entered loader state or discarded the prepared lease");
    }
    // A dependency can appear after prepare. Commit must check again before loading.
    Edges(other.AssemblyContext).Add(payload.AssemblyContext);
    Reject(() => Invoke("Commit", commit), "dependent Mod DependentFixture"); Prepared();
    Edges(other.AssemblyContext).Clear();
    other.Dependencies.Add(new EverestModuleMetadata { Name = payload.Name });
    Reject(() => Invoke("Commit", commit), "dependent Mod DependentFixture"); Prepared();
    other.Dependencies.Clear();
    Set(other.AssemblyContext, "ActiveDependencyContexts", null);
    Reject(() => Invoke("Commit", commit), "Cannot inspect active dependencies"); Prepared();
    Console.WriteLine("PASS: standard dependency inspection; declared/actual/query-failure prepare refusals; safe prepare; commit rechecks preserve lease");
    Console.WriteLine("Runtime SHA256: " + Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
        File.ReadAllBytes(typeof(BridgeRuntime).Assembly.Location))));
    Console.WriteLine("Everest assembly: " + typeof(Everest).Assembly.Location);
    Console.WriteLine("Everest SHA256: " + Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
        File.ReadAllBytes(typeof(Everest).Assembly.Location))));
} finally {
    BridgeRuntime.Shutdown(); modules.Clear();
}

sealed class FixtureModule : EverestModule {
    public override void Load() { }
    public override void Unload() { }
}
