import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getMe, updatePassword } from '@/lib/api/user';

export function useUserProfile() {
  return useQuery({
    queryKey: ['user', 'profile'],
    queryFn: getMe,
    staleTime: 5 * 60 * 1000, // 5 minutes
  });
}

export function useUpdatePassword() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      currentPassword,
      newPassword,
    }: {
      currentPassword: string;
      newPassword: string;
    }) => updatePassword(currentPassword, newPassword),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user', 'profile'] });
    },
  });
}
