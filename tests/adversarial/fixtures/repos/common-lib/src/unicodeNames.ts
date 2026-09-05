// Unicode identifiers in TypeScript (V1_SPEC §14, §7).
//
// JS/TS identifier rules are not Python's, so the parser is the authority on what
// a name is; retrieval must find whatever the parser stored, whatever its script.

export const MESSAGE_INVALIDE = "le nom fourni est invalide";

export function traiterUtilisateur(nom: string): string {
  return nom;
}

export class Ünïcode {
  traiter(nom: string): string {
    return nom;
  }
}
