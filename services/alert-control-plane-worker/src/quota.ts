type SqlValue = ArrayBuffer | string | number | null;

export type PrincipalQuotaStorage = Pick<
  DurableObjectStorage,
  "sql" | "transactionSync"
>;

export interface QuotaReservationIdentity {
  readonly environment: string;
  readonly principal: string;
  readonly incidentId: string;
  readonly generation: number;
}

export type QuotaReservationState =
  | "pending"
  | "confirmed"
  | "released"
  | "expired";

export interface QuotaReservation extends QuotaReservationIdentity {
  readonly key: string;
  readonly leaseEpoch: number;
  readonly state: QuotaReservationState;
  readonly leaseExpiresAt: number;
  readonly createdAt: number;
  readonly confirmedAt: number | null;
  readonly releasedAt: number | null;
}

export type QuotaReservationResult =
  | {
      admitted: true;
      idempotent: boolean;
      reservation: QuotaReservation;
    }
  | {
      admitted: false;
      reason: "active_limit" | "reservation_released";
      activeCount: number;
      limit: number;
    };

export type QuotaLeaseErrorCode =
  | "invalid_reservation"
  | "unknown_reservation"
  | "stale_lease"
  | "expired_lease"
  | "released_lease";

export class QuotaLeaseError extends Error {
  readonly code: QuotaLeaseErrorCode;

  constructor(code: QuotaLeaseErrorCode) {
    super(code);
    this.name = "QuotaLeaseError";
    this.code = code;
  }
}

export interface PrincipalQuotaOptions {
  readonly activeLimit: number;
  readonly pendingLeaseMs: number;
  readonly now?: () => number;
}

interface QuotaRepository {
  transaction<Result>(closure: () => Result): Result;
  find(identity: QuotaReservationIdentity): QuotaReservation | undefined;
  insert(reservation: QuotaReservation): void;
  replace(reservation: QuotaReservation): void;
  activeCount(environment: string, principal: string): number;
  reclaimExpired(
    environment: string,
    principal: string,
    now: number,
  ): number;
}

class SqlQuotaRepository implements QuotaRepository {
  constructor(private readonly storage: PrincipalQuotaStorage) {
    storage.transactionSync(() => {
      storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS principal_quota_reservations (
          environment TEXT NOT NULL,
          principal TEXT NOT NULL,
          incident_id TEXT NOT NULL,
          generation INTEGER NOT NULL,
          lease_epoch INTEGER NOT NULL,
          state TEXT NOT NULL CHECK (
            state IN ('pending', 'confirmed', 'released', 'expired')
          ),
          lease_expires_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL,
          confirmed_at INTEGER,
          released_at INTEGER,
          PRIMARY KEY (environment, principal, incident_id, generation)
        ) WITHOUT ROWID
      `);
      storage.sql.exec(`
        CREATE INDEX IF NOT EXISTS principal_quota_active
        ON principal_quota_reservations (environment, principal, state)
      `);
    });
  }

  transaction<Result>(closure: () => Result): Result {
    return this.storage.transactionSync(closure);
  }

  find(identity: QuotaReservationIdentity): QuotaReservation | undefined {
    const rows = this.storage.sql
      .exec<Record<string, SqlValue>>(
        `SELECT environment, principal, incident_id, generation, lease_epoch,
                state, lease_expires_at, created_at, confirmed_at, released_at
         FROM principal_quota_reservations
         WHERE environment = ? AND principal = ? AND incident_id = ?
           AND generation = ?
         LIMIT 1`,
        identity.environment,
        identity.principal,
        identity.incidentId,
        identity.generation,
      )
      .toArray();
    return rows[0] === undefined ? undefined : reservationFromRow(rows[0]);
  }

  insert(reservation: QuotaReservation): void {
    this.storage.sql.exec(
      `INSERT INTO principal_quota_reservations (
         environment, principal, incident_id, generation, lease_epoch, state,
         lease_expires_at, created_at, confirmed_at, released_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      reservation.environment,
      reservation.principal,
      reservation.incidentId,
      reservation.generation,
      reservation.leaseEpoch,
      reservation.state,
      reservation.leaseExpiresAt,
      reservation.createdAt,
      reservation.confirmedAt,
      reservation.releasedAt,
    );
  }

  replace(reservation: QuotaReservation): void {
    this.storage.sql.exec(
      `UPDATE principal_quota_reservations
       SET lease_epoch = ?, state = ?, lease_expires_at = ?, created_at = ?,
           confirmed_at = ?, released_at = ?
       WHERE environment = ? AND principal = ? AND incident_id = ?
         AND generation = ?`,
      reservation.leaseEpoch,
      reservation.state,
      reservation.leaseExpiresAt,
      reservation.createdAt,
      reservation.confirmedAt,
      reservation.releasedAt,
      reservation.environment,
      reservation.principal,
      reservation.incidentId,
      reservation.generation,
    );
  }

  activeCount(environment: string, principal: string): number {
    return numberColumn(
      this.storage.sql
        .exec<Record<string, SqlValue>>(
          `SELECT COUNT(*) AS active_count
           FROM principal_quota_reservations
           WHERE environment = ? AND principal = ?
             AND state IN ('pending', 'confirmed')`,
          environment,
          principal,
        )
        .one(),
      "active_count",
    );
  }

  reclaimExpired(
    environment: string,
    principal: string,
    now: number,
  ): number {
    return this.storage.sql.exec(
      `UPDATE principal_quota_reservations
       SET state = 'expired'
       WHERE environment = ? AND principal = ? AND state = 'pending'
         AND lease_expires_at <= ?`,
      environment,
      principal,
      now,
    ).rowsWritten;
  }
}

class MemoryQuotaRepository implements QuotaRepository {
  private readonly reservations = new Map<string, QuotaReservation>();

  transaction<Result>(closure: () => Result): Result {
    return closure();
  }

  find(identity: QuotaReservationIdentity): QuotaReservation | undefined {
    return this.reservations.get(reservationStorageKey(identity));
  }

  insert(reservation: QuotaReservation): void {
    this.reservations.set(reservationStorageKey(reservation), reservation);
  }

  replace(reservation: QuotaReservation): void {
    this.reservations.set(reservationStorageKey(reservation), reservation);
  }

  activeCount(environment: string, principal: string): number {
    let count = 0;
    for (const reservation of this.reservations.values()) {
      if (
        reservation.environment === environment &&
        reservation.principal === principal &&
        (reservation.state === "pending" ||
          reservation.state === "confirmed")
      ) {
        count += 1;
      }
    }
    return count;
  }

  reclaimExpired(
    environment: string,
    principal: string,
    now: number,
  ): number {
    let reclaimed = 0;
    for (const [key, reservation] of this.reservations.entries()) {
      if (
        reservation.environment === environment &&
        reservation.principal === principal &&
        reservation.state === "pending" &&
        reservation.leaseExpiresAt <= now
      ) {
        this.reservations.set(
          key,
          freezeReservation({ ...reservation, state: "expired" }),
        );
        reclaimed += 1;
      }
    }
    return reclaimed;
  }
}

class PrincipalQuotaCore {
  private readonly activeLimit: number;
  private readonly pendingLeaseMs: number;
  private readonly clock: () => number;

  constructor(
    private readonly repository: QuotaRepository,
    options: PrincipalQuotaOptions,
  ) {
    if (!Number.isSafeInteger(options.activeLimit) || options.activeLimit < 1) {
      throw new QuotaLeaseError("invalid_reservation");
    }
    if (
      !Number.isSafeInteger(options.pendingLeaseMs) ||
      options.pendingLeaseMs < 1
    ) {
      throw new QuotaLeaseError("invalid_reservation");
    }
    this.activeLimit = options.activeLimit;
    this.pendingLeaseMs = options.pendingLeaseMs;
    this.clock = options.now ?? Date.now;
  }

  reserve(identity: QuotaReservationIdentity): QuotaReservationResult {
    validateIdentity(identity);
    const now = this.safeNow();
    const leaseExpiresAt = safeTimestampAdd(now, this.pendingLeaseMs);

    return this.repository.transaction(() => {
      this.repository.reclaimExpired(
        identity.environment,
        identity.principal,
        now,
      );
      const existing = this.repository.find(identity);
      if (
        existing?.state === "pending" ||
        existing?.state === "confirmed"
      ) {
        return {
          admitted: true,
          idempotent: true,
          reservation: existing,
        };
      }
      const count = this.repository.activeCount(
        identity.environment,
        identity.principal,
      );
      if (existing?.state === "released") {
        return {
          admitted: false,
          reason: "reservation_released",
          activeCount: count,
          limit: this.activeLimit,
        };
      }
      if (count >= this.activeLimit) {
        return {
          admitted: false,
          reason: "active_limit",
          activeCount: count,
          limit: this.activeLimit,
        };
      }

      const reservation = freezeReservation({
        ...identity,
        key: quotaReservationKey(identity.incidentId, identity.generation),
        leaseEpoch: (existing?.leaseEpoch ?? 0) + 1,
        state: "pending",
        leaseExpiresAt,
        createdAt: now,
        confirmedAt: null,
        releasedAt: null,
      });
      if (existing === undefined) {
        this.repository.insert(reservation);
      } else {
        this.repository.replace(reservation);
      }
      return { admitted: true, idempotent: false, reservation };
    });
  }

  confirm(
    identity: QuotaReservationIdentity,
    leaseEpoch: number,
  ): QuotaReservation {
    validateIdentity(identity);
    validateLeaseEpoch(leaseEpoch);
    const now = this.safeNow();

    return this.repository.transaction(() => {
      const existing = this.requireCurrent(identity, leaseEpoch);
      if (existing.state === "confirmed") {
        return existing;
      }
      if (existing.state === "released") {
        throw new QuotaLeaseError("released_lease");
      }
      if (
        existing.state === "expired" ||
        existing.leaseExpiresAt <= now
      ) {
        if (existing.state === "pending") {
          this.repository.replace(
            freezeReservation({ ...existing, state: "expired" }),
          );
        }
        throw new QuotaLeaseError("expired_lease");
      }

      const confirmed = freezeReservation({
        ...existing,
        state: "confirmed",
        confirmedAt: now,
      });
      this.repository.replace(confirmed);
      return confirmed;
    });
  }

  release(
    identity: QuotaReservationIdentity,
    leaseEpoch: number,
  ): QuotaReservation {
    validateIdentity(identity);
    validateLeaseEpoch(leaseEpoch);
    const now = this.safeNow();

    return this.repository.transaction(() => {
      const existing = this.requireCurrent(identity, leaseEpoch);
      if (existing.state === "released" || existing.state === "expired") {
        return existing;
      }
      const released = freezeReservation({
        ...existing,
        state: "released",
        releasedAt: now,
      });
      this.repository.replace(released);
      return released;
    });
  }

  reclaimExpired(environment: string, principal: string): number {
    validateScope(environment, principal);
    const now = this.safeNow();
    return this.repository.transaction(() =>
      this.repository.reclaimExpired(environment, principal, now),
    );
  }

  activeCount(environment: string, principal: string): number {
    validateScope(environment, principal);
    const now = this.safeNow();
    return this.repository.transaction(() => {
      this.repository.reclaimExpired(environment, principal, now);
      return this.repository.activeCount(environment, principal);
    });
  }

  private requireCurrent(
    identity: QuotaReservationIdentity,
    leaseEpoch: number,
  ): QuotaReservation {
    const existing = this.repository.find(identity);
    if (existing === undefined) {
      throw new QuotaLeaseError("unknown_reservation");
    }
    if (existing.leaseEpoch !== leaseEpoch) {
      throw new QuotaLeaseError("stale_lease");
    }
    return existing;
  }

  private safeNow(): number {
    const now = this.clock();
    if (!Number.isSafeInteger(now) || now < 0) {
      throw new QuotaLeaseError("invalid_reservation");
    }
    return now;
  }
}

export class PrincipalQuota extends PrincipalQuotaCore {
  constructor(storage: PrincipalQuotaStorage, options: PrincipalQuotaOptions) {
    super(new SqlQuotaRepository(storage), options);
  }
}

export class InMemoryPrincipalQuota extends PrincipalQuotaCore {
  constructor(options: PrincipalQuotaOptions) {
    super(new MemoryQuotaRepository(), options);
  }
}

export function quotaReservationKey(
  incidentId: string,
  generation: number,
): string {
  return `${incidentId}:${generation}`;
}

function validateIdentity(identity: QuotaReservationIdentity): void {
  validateScope(identity.environment, identity.principal);
  if (
    typeof identity.incidentId !== "string" ||
    identity.incidentId.length === 0 ||
    identity.incidentId.length > 256 ||
    !/^[A-Za-z0-9._-]+$/.test(identity.incidentId) ||
    !Number.isSafeInteger(identity.generation) ||
    identity.generation < 1
  ) {
    throw new QuotaLeaseError("invalid_reservation");
  }
}

function validateScope(environment: string, principal: string): void {
  if (
    typeof environment !== "string" ||
    environment.length === 0 ||
    environment.length > 64 ||
    !/^[a-z][a-z0-9-]*$/.test(environment) ||
    typeof principal !== "string" ||
    principal.length === 0 ||
    principal.length > 256 ||
    !/^[A-Za-z0-9._:@/-]+$/.test(principal)
  ) {
    throw new QuotaLeaseError("invalid_reservation");
  }
}

function validateLeaseEpoch(leaseEpoch: number): void {
  if (!Number.isSafeInteger(leaseEpoch) || leaseEpoch < 1) {
    throw new QuotaLeaseError("invalid_reservation");
  }
}

function safeTimestampAdd(timestamp: number, duration: number): number {
  const result = timestamp + duration;
  if (!Number.isSafeInteger(result)) {
    throw new QuotaLeaseError("invalid_reservation");
  }
  return result;
}

function reservationStorageKey(identity: QuotaReservationIdentity): string {
  return JSON.stringify([
    identity.environment,
    identity.principal,
    identity.incidentId,
    identity.generation,
  ]);
}

function reservationFromRow(
  row: Record<string, SqlValue>,
): QuotaReservation {
  const incidentId = stringColumn(row, "incident_id");
  const generation = numberColumn(row, "generation");
  return freezeReservation({
    environment: stringColumn(row, "environment"),
    principal: stringColumn(row, "principal"),
    incidentId,
    generation,
    key: quotaReservationKey(incidentId, generation),
    leaseEpoch: numberColumn(row, "lease_epoch"),
    state: reservationStateColumn(row, "state"),
    leaseExpiresAt: numberColumn(row, "lease_expires_at"),
    createdAt: numberColumn(row, "created_at"),
    confirmedAt: nullableNumberColumn(row, "confirmed_at"),
    releasedAt: nullableNumberColumn(row, "released_at"),
  });
}

function freezeReservation(
  reservation: QuotaReservation,
): QuotaReservation {
  return Object.freeze(reservation);
}

function stringColumn(row: Record<string, SqlValue>, column: string): string {
  const value = row[column];
  if (typeof value !== "string") {
    throw new Error(`invalid quota row: ${column}`);
  }
  return value;
}

function numberColumn(row: Record<string, SqlValue>, column: string): number {
  const value = row[column];
  if (typeof value !== "number") {
    throw new Error(`invalid quota row: ${column}`);
  }
  return value;
}

function nullableNumberColumn(
  row: Record<string, SqlValue>,
  column: string,
): number | null {
  const value = row[column];
  if (value !== null && typeof value !== "number") {
    throw new Error(`invalid quota row: ${column}`);
  }
  return value;
}

function reservationStateColumn(
  row: Record<string, SqlValue>,
  column: string,
): QuotaReservationState {
  const value = stringColumn(row, column);
  if (
    value !== "pending" &&
    value !== "confirmed" &&
    value !== "released" &&
    value !== "expired"
  ) {
    throw new Error(`invalid quota row: ${column}`);
  }
  return value;
}
