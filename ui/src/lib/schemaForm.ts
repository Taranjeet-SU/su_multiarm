// A form model derived from a tool's input JSON Schema (``GET /v2/tools``), so the log-intent
// shortcuts follow whatever input models the server ships, with no field list hard-coded here.

export type FieldKind = "enum" | "string" | "integer" | "number" | "boolean" | "string_list" | "json";

export interface FormField {
  name: string;
  kind: FieldKind;
  required: boolean;
  options: string[];
  default: unknown;
  description: string;
}

type Schema = Record<string, unknown>;

function resolve(schema: Schema, root: Schema): Schema {
  const ref = schema.$ref;
  if (typeof ref === "string" && ref.startsWith("#/$defs/")) {
    const defs = (root.$defs ?? {}) as Record<string, Schema>;
    return defs[ref.slice("#/$defs/".length)] ?? schema;
  }
  return schema;
}

function nonNull(schema: Schema, root: Schema): Schema {
  const resolved = resolve(schema, root);
  const any = (resolved.anyOf ?? resolved.oneOf) as Schema[] | undefined;
  if (Array.isArray(any)) {
    const rest = any.map((s) => resolve(s, root)).filter((s) => s.type !== "null");
    if (rest.length === 1) return rest[0];
  }
  return resolved;
}

export function formFields(root: Schema): FormField[] {
  const properties = (root.properties ?? {}) as Record<string, Schema>;
  const required = new Set((root.required ?? []) as string[]);
  return Object.entries(properties).map(([name, raw]) => {
    const schema = nonNull(raw, root);
    const description = typeof raw.description === "string" ? raw.description : typeof schema.description === "string" ? schema.description : "";
    const base = { name, required: required.has(name), default: raw.default, description, options: [] as string[] };
    if (Array.isArray(schema.enum)) return { ...base, kind: "enum", options: schema.enum.map(String) };
    if (schema.const !== undefined) return { ...base, kind: "enum", options: [String(schema.const)] };
    if (schema.type === "string") return { ...base, kind: "string" };
    if (schema.type === "integer") return { ...base, kind: "integer" };
    if (schema.type === "number") return { ...base, kind: "number" };
    if (schema.type === "boolean") return { ...base, kind: "boolean" };
    if (schema.type === "array") {
      const items = nonNull((schema.items ?? {}) as Schema, root);
      if (items.type === "string" || Array.isArray(items.enum)) return { ...base, kind: "string_list", options: Array.isArray(items.enum) ? items.enum.map(String) : [] };
    }
    return { ...base, kind: "json" };
  });
}

/** Form values (strings) to tool arguments. Empty optional values are omitted (server defaults
 * apply); a malformed number or JSON value throws, it is never coerced. */
export function toArgs(fields: FormField[], values: Record<string, string>): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  for (const f of fields) {
    const raw = (values[f.name] ?? "").trim();
    if (raw === "") {
      if (f.required) throw new Error(`${f.name} is required`);
      continue;
    }
    switch (f.kind) {
      case "integer":
      case "number": {
        const n = Number(raw);
        if (!Number.isFinite(n) || (f.kind === "integer" && !Number.isInteger(n))) throw new Error(`${f.name} must be ${f.kind === "integer" ? "an integer" : "a number"}`);
        args[f.name] = n;
        break;
      }
      case "boolean":
        args[f.name] = raw === "true";
        break;
      case "string_list":
        args[f.name] = raw.split(",").map((s) => s.trim()).filter(Boolean);
        break;
      case "json":
        try {
          args[f.name] = JSON.parse(raw);
        } catch {
          throw new Error(`${f.name} must be JSON`);
        }
        break;
      default:
        args[f.name] = raw;
    }
  }
  return args;
}

export function initialValues(fields: FormField[], presets: Record<string, string> = {}): Record<string, string> {
  const values: Record<string, string> = {};
  for (const f of fields) {
    if (presets[f.name] !== undefined) values[f.name] = presets[f.name];
    else if (f.default !== undefined && f.default !== null) values[f.name] = Array.isArray(f.default) ? f.default.join(", ") : typeof f.default === "object" ? JSON.stringify(f.default) : String(f.default);
    else values[f.name] = "";
  }
  return values;
}
