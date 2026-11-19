/** Capability set negotiated by `hello`. */
export class FeatureSet {
  private readonly names: Set<string>;

  constructor(names: readonly string[]) {
    this.names = new Set(names);
  }

  has(name: string): boolean {
    return this.names.has(name);
  }

  get size(): number {
    return this.names.size;
  }

  toArray(): string[] {
    return [...this.names];
  }
}
