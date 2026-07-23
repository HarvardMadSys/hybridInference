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
  insert(record: TrustedDeploymentMetadata): void;
  retire(key: DeploymentKey, retiredAt: number, registryVersion: number): void;
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
        return this.activate(command.deployment);
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

  version(): number {
    return this.repository.transaction(() => this.repository.version());
  }

  private activate(
    deployment: Omit<
      TrustedDeploymentMetadata,
      "retiredAt" | "registryVersion"
    >,
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
      return exact;
    }

    const record = freezeDeployment({
      ...deployment,
      retiredAt: null,
      registryVersion: this.repository.nextVersion(),
    });
    this.repository.insert(record);
    return record;
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
  validateName(key.environment, 64);
  validateName(key.service, 128);
  validateOpaqueIdentifier(key.deploymentId, 256);
  if (!/^[a-z0-9][a-z0-9._+-]{0,31}:[a-f0-9]{32,256}$/.test(key.artifactDigest)) {
    throw new DeploymentRegistryWriteError("invalid_attestation");
  }
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
