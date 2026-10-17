'use client';

import React from 'react';
import clsx from 'clsx';
import { userDisplayName, type UserIdentity } from '@/lib/utils/userLabel';

type AvatarSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl';

const SIZE_CLASSES: Record<AvatarSize, string> = {
  xs: 'h-6 w-6 text-xs',
  sm: 'h-7 w-7 text-xs',
  md: 'h-10 w-10 text-sm',
  lg: 'h-14 w-14 text-lg',
  xl: 'h-16 w-16 text-xl',
};

export function getUserInitials(name: string): string {
  const cleaned = name.trim();
  if (!cleaned) return 'U';
  const parts = cleaned.split(/[\s._-]+/).filter(Boolean);
  if (parts.length >= 2) {
    return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  }
  return cleaned.slice(0, 2).toUpperCase();
}

export function UserAvatar({
  user,
  avatarUrl,
  size = 'md',
  className,
}: {
  user?: (UserIdentity & { avatar_url?: string | null }) | null;
  avatarUrl?: string | null;
  size?: AvatarSize;
  className?: string;
}): JSX.Element {
  const resolvedUrl = avatarUrl !== undefined ? avatarUrl : user?.avatar_url;
  const displayName = userDisplayName(user);
  const initials = getUserInitials(displayName);

  if (resolvedUrl && resolvedUrl.startsWith('data:image/')) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={resolvedUrl}
        alt={`${displayName || 'User'} portrait`}
        className={clsx(
          'shrink-0 rounded-full object-cover ring-1 ring-gray-900/10',
          SIZE_CLASSES[size],
          className,
        )}
      />
    );
  }

  return (
    <span
      aria-label={`${displayName || 'User'} portrait`}
      className={clsx(
        'inline-flex shrink-0 select-none items-center justify-center rounded-full bg-gray-900 font-semibold tracking-tight text-white ring-1 ring-inset ring-black/10',
        SIZE_CLASSES[size],
        className,
      )}
    >
      {initials}
    </span>
  );
}

/**
 * Center-crop and resize an uploaded image file to a compact square Data URL.
 */
export async function cropImageToDataUrl(file: File, targetSize = 128): Promise<string> {
  if (!file.type.startsWith('image/')) {
    throw new Error('Please choose a PNG, JPEG, or WebP image.');
  }
  if (file.size > 8 * 1024 * 1024) {
    throw new Error('Image file must be smaller than 8 MB.');
  }

  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('Failed to read image file.'));
    reader.readAsDataURL(file);
  });

  const img = await new Promise<HTMLImageElement>((resolve, reject) => {
    const element = new Image();
    element.onload = () => resolve(element);
    element.onerror = () => reject(new Error('Failed to decode image.'));
    element.src = dataUrl;
  });

  const canvas = document.createElement('canvas');
  canvas.width = targetSize;
  canvas.height = targetSize;
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    throw new Error('Canvas is unavailable in this browser.');
  }

  const minSide = Math.min(img.width, img.height);
  const sx = Math.max(0, Math.floor((img.width - minSide) / 2));
  const sy = Math.max(0, Math.floor((img.height - minSide) / 2));
  ctx.drawImage(img, sx, sy, minSide, minSide, 0, 0, targetSize, targetSize);

  const webp = canvas.toDataURL('image/webp', 0.85);
  if (webp.startsWith('data:image/webp')) {
    return webp;
  }
  return canvas.toDataURL('image/jpeg', 0.85);
}
