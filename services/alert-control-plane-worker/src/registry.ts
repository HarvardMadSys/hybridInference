type SqlValue = ArrayBuffer | string | number | null;

export type DeploymentRegistryStorage = Pick<
  DurableObjectStorage,
  "sql" | "transactionSync"
>;

export interface DeploymentKey {
  readonly environment: string;
  readonly service: string;
  readonly deploymentId: string;
  readonly artifactDigest: string;
}

export type DeploymentIdentity = Pick<
  DeploymentKey,
  "environment" | "service" | "deploymentId"
>;

export interface TrustedDeploymentMetadata extends DeploymentKey {
  readonly deploymentSha: string;
  readonly activatedAt: number;
  readonly retiredAt: number | null;
  readonly registryVersion: number;
}

export type VerifiedDeploymentCommand =
  | {
      readonly action: "activate";
      readonly deployment: Omit<
        TrustedDeploymentMetadata,
        "retiredAt" | "registryVersion"
      >;
      /**
       * Retire whatever else this service still has active. Only correct where
       * exactly one deployment can be serving — a single long-lived process.
       * A Worker under a gradual rollout genuinely runs two versions at once,
       * so superseding there would reject alerts from the half still serving.
       */
      readonly supersedes?: boolean;
    }
  | {
      readonly action: "retire";
      readonly deployment: DeploymentKey;
      readonly retiredAt: number;
    };

export interface DeploymentAttestationVerifier<Attestation> {
  verify(
    attestation: Attestation,
  ): VerifiedDeploymentCommand | Promise<VerifiedDeploymentCommand>;
}

export type DeploymentLookupErrorCode =
  | "unknown_deployment"
  | "retired_deployment"
  | "deployment_mismatch";

export class DeploymentLookupError extends Error {
  readonly code: DeploymentLookupErrorCode;

  constructor(code: DeploymentLookupErrorCode) {
    super(code);
    this.name = "DeploymentLookupError";
    this.code = code;
  }
}

export type DeploymentRegistryWriteErrorCode =
  | "invalid_attestation"
  | "deployment_conflict"
  | "deployment_retired";

export class DeploymentRegistryWriteError extends Error {
  readonly code: DeploymentRegistryWriteErrorCode;

  constructor(code: DeploymentRegistryWriteErrorCode) {
    super(code);
    this.name = "DeploymentRegistryWriteError";
    this.code = code;
  }
}

interface RegistryRepository {
  transaction<Result>(closure: () => Result): Result;
  findExact(key: DeploymentKey): TrustedDeploymentMetadata | undefined;
  findByDeploymentId(
    deploymentId: string,
  ): TrustedDeploymentMetadata | undefined;
  countByDeploymentId(deploymentId: string): number;
  insert(record: TrustedDeploymentMetadata): void;
  retire(key: DeploymentKey, retiredAt: number, registryVersion: number): void;
  retireSuperseded(
    survivor: DeploymentKey,
    retiredAt: number,
    allocateVersion: () => number,
  ): void;
  nextVersion(): number;
  version(): number;
}

class SqlRegistryRepository implements RegistryRepository {
  constructor(private readonly storage: DeploymentRegistryStorage) {
    storage.transactionSync(() => {
      storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS deployment_registry (
          environment TEXT NOT NULL,
          service TEXT NOT NULL,
          deployment_id TEXT NOT NULL,
          artifact_digest TEXT NOT NULL,
          deployment_sha TEXT NOT NULL,
          activated_at INTEGER NOT NULL,
          retired_at INTEGER,
          registry_version INTEGER NOT NULL,
          PRIMARY KEY (environment, service, deployment_id, artifact_digest)
        ) WITHOUT ROWID
      `);
      storage.sql.exec(`
        CREATE INDEX IF NOT EXISTS deployment_registry_deployment_id
        ON deployment_registry (deployment_id)
      `);
      storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS deployment_registry_metadata (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          version INTEGER NOT NULL
        )
      `);
      storage.sql.exec(
        `INSERT OR IGNORE INTO deployment_registry_metadata (singleton, version)
         VALUES (1, 0)`,
      );
    });
  }

  transaction<Result>(closure: () => Result): Result {
    return this.storage.transactionSync(closure);
  }

  findExact(key: DeploymentKey): TrustedDeploymentMetadata | undefined {
    const rows = this.storage.sql
      .exec<Record<string, SqlValue>>(
        `SELECT environment, service, deployment_id, artifact_digest,
                deployment_sha, activated_at, retired_at, registry_version
         FROM deployment_registry
         WHERE environment = ? AND service = ? AND deployment_id = ?
           AND artifact_digest = ?
         LIMIT 1`,
        key.environment,
        key.service,
        key.deploymentId,
        key.artifactDigest,
      )
      .toArray();
    return rows[0] === undefined ? undefined : deploymentFromRow(rows[0]);
  }

  findByDeploymentId(
    deploymentId: string,
  ): TrustedDeploymentMetadata | undefined {
    const rows = this.storage.sql
      .exec<Record<string, SqlValue>>(
        `SELECT environment, service, deployment_id, artifact_digest,
                deployment_sha, activated_at, retired_at, registry_version
         FROM deployment_registry
         WHERE deployment_id = ?
         ORDER BY registry_version DESC
         LIMIT 1`,
        deploymentId,
      )
      .toArray();
    return rows[0] === undefined ? undefined : deploymentFromRow(rows[0]);
  }

  countByDeploymentId(deploymentId: string): number {
    return numberColumn(
      this.storage.sql
        .exec<Record<string, SqlValue>>(
          `SELECT COUNT(*) AS count
           FROM deployment_registry
           WHERE deployment_id = ?`,
          deploymentId,
        )
        .one(),
      "count",
    );
  }

  insert(record: TrustedDeploymentMetadata): void {
    this.storage.sql.exec(
      `INSERT INTO deployment_registry (
         environment, service, deployment_id, artifact_digest, deployment_sha,
         activated_at, retired_at, registry_version
       ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)`,
      record.environment,
      record.service,
      record.deploymentId,
      record.artifactDigest,
      record.deploymentSha,
      record.activatedAt,
      record.registryVersion,
    );
  }

  retire(
    key: DeploymentKey,
    retiredAt: number,
    registryVersion: number,
  ): void {
    this.storage.sql.exec(
      `UPDATE deployment_registry
       SET retired_at = ?, registry_version = ?
       WHERE environment = ? AND service = ? AND deployment_id = ?
         AND artifact_digest = ? AND retired_at IS NULL`,
      retiredAt,
      registryVersion,
      key.environment,
      key.service,
      key.deploymentId,
      key.artifactDigest,
    );
  }

  retireSuperseded(
    survivor: DeploymentKey,
    retiredAt: number,
    allocateVersion: () => number,
  ): void {
    const doomed = this.storage.sql
      .exec<Record<string, SqlValue>>(
        `SELECT COUNT(*) AS count
         FROM deployment_registry
         WHERE environment = ? AND service = ? AND retired_at IS NULL
           AND activated_at <= ?
           AND NOT (deployment_id = ? AND artifact_digest = ?)`,
        survivor.environment,
        survivor.service,
        retiredAt,
        survivor.deploymentId,
        survivor.artifactDigest,
      )
      .one();
    // Allocating unconditionally would burn a registry version on every deploy
    // that supersedes nothing, and the version is what a capability is pinned
    // to — so it stays a number that only moves when a record does.
    if (numberColumn(doomed, "count") === 0) return;
    this.storage.sql.exec(
      `UPDATE deployment_registry
       SET retired_at = ?, registry_version = ?
       WHERE environment = ? AND service = ? AND retired_at IS NULL
         AND activated_at <= ?
         AND NOT (deployment_id = ? AND artifact_digest = ?)`,
      retiredAt,
      allocateVersion(),
      survivor.environment,
      survivor.service,
      retiredAt,
      survivor.deploymentId,
      survivor.artifactDigest,
    );
  }

  nextVersion(): number {
    return numberColumn(
      this.storage.sql
        .exec<Record<string, SqlValue>>(
          `UPDATE deployment_registry_metadata
           SET version = version + 1
           WHERE singleton = 1
           RETURNING version`,
        )
        .one(),
      "version",
    );
  }

  version(): number {
    return numberColumn(
      this.storage.sql
        .exec<Record<string, SqlValue>>(
          `SELECT version FROM deployment_registry_metadata WHERE singleton = 1`,
        )
        .one(),
      "version",
    );
  }
}

class MemoryRegistryRepository implements RegistryRepository {
  private readonly records = new Map<string, TrustedDeploymentMetadata>();
  private registryVersion = 0;

  transaction<Result>(closure: () => Result): Result {
    return closure();
  }

  findExact(key: DeploymentKey): TrustedDeploymentMetadata | undefined {
    return this.records.get(deploymentKey(key));
  }

  findByDeploymentId(
    deploymentId: string,
  ): TrustedDeploymentMetadata | undefined {
    let match: TrustedDeploymentMetadata | undefined;
    for (const record of this.records.values()) {
      if (
        record.deploymentId === deploymentId &&
        (match === undefined || record.registryVersion > match.registryVersion)
      ) {
        match = record;
      }
    }
    return match;
  }

  countByDeploymentId(deploymentId: string): number {
    let count = 0;
    for (const record of this.records.values()) {
      if (record.deploymentId === deploymentId) count += 1;
    }
    return count;
  }

  insert(record: TrustedDeploymentMetadata): void {
    this.records.set(deploymentKey(record), record);
  }

  retire(
    key: DeploymentKey,
    retiredAt: number,
    registryVersion: number,
  ): void {
    const existing = this.records.get(deploymentKey(key));
    if (existing !== undefined && existing.retiredAt === null) {
      this.records.set(
        deploymentKey(key),
        freezeDeployment({ ...existing, retiredAt, registryVersion }),
      );
    }
  }

  retireSuperseded(
    survivor: DeploymentKey,
    retiredAt: number,
    allocateVersion: () => number,
  ): void {
    const survivorKey = deploymentKey(survivor);
    const doomed = [...this.records].filter(
      ([key, record]) =>
        key !== survivorKey &&
        record.retiredAt === null &&
        record.environment === survivor.environment &&
        record.service === survivor.service &&
        record.activatedAt <= retiredAt,
    );
    if (doomed.length === 0) return;
    const registryVersion = allocateVersion();
    for (const [key, record] of doomed) {
      this.records.set(
        key,
        freezeDeployment({ ...record, retiredAt, registryVersion }),
      );
    }
  }

  nextVersion(): number {
    this.registryVersion += 1;
    return this.registryVersion;
  }

  version(): number {
    return this.registryVersion;
  }
}

class DeploymentRegistryCore<Attestation> {
  constructor(
    private readonly repository: RegistryRepository,
    private readonly verifier: DeploymentAttestationVerifier<Attestation>,
  ) {}

  async apply(attestation: Attestation): Promise<TrustedDeploymentMetadata> {
    const command = await this.verifier.verify(attestation);
    validateCommand(command);

    return this.repository.transaction(() => {
      if (command.action === "activate") {
        return this.activate(command.deployment, command.supersedes === true);
      }
      return this.retire(command.deployment, command.retiredAt);
    });
  }

  lookup(key: DeploymentKey): TrustedDeploymentMetadata {
    validateDeploymentKey(key);

    return this.repository.transaction(() => {
      const exact = this.repository.findExact(key);
      if (exact !== undefined) {
        if (exact.retiredAt !== null) {
          throw new DeploymentLookupError("retired_deployment");
        }
        return exact;
      }

      if (this.repository.findByDeploymentId(key.deploymentId) !== undefined) {
        throw new DeploymentLookupError("deployment_mismatch");
      }
      throw new DeploymentLookupError("unknown_deployment");
    });
  }

  /**
   * Resolve an active immutable deployment when the platform supplies only its
   * version ID. The environment and service remain mandatory so an ID cannot
   * cross a role boundary even if a registry shard is called incorrectly.
   */
  lookupByDeploymentId(
    identity: DeploymentIdentity,
  ): TrustedDeploymentMetadata {
    validateDeploymentIdentity(identity);

    return this.repository.transaction(() => {
      const deployment = this.repository.findByDeploymentId(
        identity.deploymentId,
      );
      if (deployment === undefined) {
        throw new DeploymentLookupError("unknown_deployment");
      }
      // A platform version ID must resolve to exactly one attested artifact.
      // The legacy exact-key lookup still supports rolling records that share
      // an ID, but this reduced-key RPC must never select one ambiguously.
      if (
        this.repository.countByDeploymentId(identity.deploymentId) !== 1
      ) {
        throw new DeploymentLookupError("deployment_mismatch");
      }
      if (
        deployment.environment !== identity.environment ||
        deployment.service !== identity.service
      ) {
        throw new DeploymentLookupError("deployment_mismatch");
      }
      if (deployment.retiredAt !== null) {
        throw new DeploymentLookupError("retired_deployment");
      }
      return deployment;
    });
  }

  version(): number {
    return this.repository.transaction(() => this.repository.version());
  }

  private activate(
    deployment: Omit<
      TrustedDeploymentMetadata,
      "retiredAt" | "registryVersion"
    >,
    supersedes: boolean,
  ): TrustedDeploymentMetadata {
    const key: DeploymentKey = deployment;
    const exact = this.repository.findExact(key);
    if (exact !== undefined) {
      if (exact.retiredAt !== null) {
        throw new DeploymentRegistryWriteError("deployment_retired");
      }
      if (
        exact.deploymentSha !== deployment.deploymentSha ||
        exact.activatedAt !== deployment.activatedAt
      ) {
        throw new DeploymentRegistryWriteError("deployment_conflict");
      }
      // A retry that adds `supersedes` must still take effect. Returning here
      // would leave the earlier deployments this command asks to retire active
      // until some later deployment happened to supersede them.
      this.supersede(exact, supersedes);
      return exact;
    }

    const record = freezeDeployment({
      ...deployment,
      retiredAt: null,
      registryVersion: this.repository.nextVersion(),
    });
    this.repository.insert(record);
    this.supersede(record, supersedes);
    return record;
  }

  /**
   * Retire what this deployment replaced. Without it the previous record stays
   * active forever, so a capability minted for it keeps authenticating long
   * after that code stopped being deployed — leaving token expiry as the only
   * revocation there is.
   *
   * Only records that activated no later than the survivor are retired.
   * Attestations are accepted anywhere inside a clock-skew window and carry no
   * ordering, so a delayed activation can arrive after a newer one; unbounded,
   * it would retire the deployment that actually superseded *it*, leaving the
   * stale one as the sole survivor and killing the live one's capability. The
   * bound also keeps a written `retiredAt` from preceding its own record's
   * `activatedAt`.
   */
  private supersede(
    survivor: TrustedDeploymentMetadata,
    supersedes: boolean,
  ): void {
    if (!supersedes) return;
    this.repository.retireSuperseded(survivor, survivor.activatedAt, () =>
      this.repository.nextVersion(),
    );
  }

  private retire(
    key: DeploymentKey,
    retiredAt: number,
  ): TrustedDeploymentMetadata {
    const exact = this.repository.findExact(key);
    if (exact === undefined) {
      if (this.repository.findByDeploymentId(key.deploymentId) !== undefined) {
        throw new DeploymentLookupError("deployment_mismatch");
      }
      throw new DeploymentLookupError("unknown_deployment");
    }
    if (retiredAt < exact.activatedAt) {
      throw new DeploymentRegistryWriteError("invalid_attestation");
    }
    if (exact.retiredAt !== null) {
      return exact;
    }

    const retired = freezeDeployment({
      ...exact,
      retiredAt,
      registryVersion: this.repository.nextVersion(),
    });
    this.repository.retire(key, retiredAt, retired.registryVersion);
    return retired;
  }
}

export class DeploymentRegistry<Attestation> extends DeploymentRegistryCore<Attestation> {
  constructor(
    storage: DeploymentRegistryStorage,
    verifier: DeploymentAttestationVerifier<Attestation>,
  ) {
    super(new SqlRegistryRepository(storage), verifier);
  }
}

export class InMemoryDeploymentRegistry<
  Attestation,
> extends DeploymentRegistryCore<Attestation> {
  constructor(verifier: DeploymentAttestationVerifier<Attestation>) {
    super(new MemoryRegistryRepository(), verifier);
  }
}

function validateCommand(command: VerifiedDeploymentCommand): void {
  if (command === null || typeof command !== "object") {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
  if (command.action === "activate") {
    validateDeploymentKey(command.deployment);
    validateSha(command.deployment.deploymentSha);
    validateTimestamp(command.deployment.activatedAt);
    return;
  }
  if (command.action === "retire") {
    validateDeploymentKey(command.deployment);
    validateTimestamp(command.retiredAt);
    return;
  }
  throw new DeploymentRegistryWriteError("invalid_attestation");
}

function validateDeploymentKey(key: DeploymentKey): void {
  validateDeploymentIdentity(key);
  if (!/^[a-z0-9][a-z0-9._+-]{0,31}:[a-f0-9]{32,256}$/.test(key.artifactDigest)) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
}

function validateDeploymentIdentity(identity: DeploymentIdentity): void {
  validateName(identity.environment, 64);
  validateName(identity.service, 128);
  validateOpaqueIdentifier(identity.deploymentId, 256);
}

function validateName(value: string, maximumLength: number): void {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximumLength ||
    !/^[a-z][a-z0-9-]*$/.test(value)
  ) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
}

function validateOpaqueIdentifier(value: string, maximumLength: number): void {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximumLength ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
}

function validateSha(value: string): void {
  if (!/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(value)) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
}

function validateTimestamp(value: number): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
}

function deploymentKey(key: DeploymentKey): string {
  return JSON.stringify([
    key.environment,
    key.service,
    key.deploymentId,
    key.artifactDigest,
  ]);
}

function deploymentFromRow(
  row: Record<string, SqlValue>,
): TrustedDeploymentMetadata {
  return freezeDeployment({
    environment: stringColumn(row, "environment"),
    service: stringColumn(row, "service"),
    deploymentId: stringColumn(row, "deployment_id"),
    artifactDigest: stringColumn(row, "artifact_digest"),
    deploymentSha: stringColumn(row, "deployment_sha"),
    activatedAt: numberColumn(row, "activated_at"),
    retiredAt: nullableNumberColumn(row, "retired_at"),
    registryVersion: numberColumn(row, "registry_version"),
  });
}

function freezeDeployment(
  deployment: TrustedDeploymentMetadata,
): TrustedDeploymentMetadata {
  return Object.freeze(deployment);
}

function stringColumn(row: Record<string, SqlValue>, column: string): string {
  const value = row[column];
  if (typeof value !== "string") {
    throw new Error(`invalid registry row: ${column}`);
  }
  return value;
}

function numberColumn(row: Record<string, SqlValue>, column: string): number {
  const value = row[column];
  if (typeof value !== "number") {
    throw new Error(`invalid registry row: ${column}`);
  }
  return value;
}

function nullableNumberColumn(
  row: Record<string, SqlValue>,
  column: string,
): number | null {
  const value = row[column];
  if (value !== null && typeof value !== "number") {
    throw new Error(`invalid registry row: ${column}`);
  }
  return value;
}
