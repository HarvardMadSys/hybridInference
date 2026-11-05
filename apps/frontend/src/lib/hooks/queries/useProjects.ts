import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createProject,
  deleteProject,
  listProjects,
  updateProject,
  type ProjectInput,
} from '@/lib/api/user';

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: listProjects,
    staleTime: 60 * 1000,
  });
}

function invalidateProjectViews(queryClient: ReturnType<typeof useQueryClient>): void {
  queryClient.invalidateQueries({ queryKey: ['projects'] });
  queryClient.invalidateQueries({ queryKey: ['apiKeys'] });
  queryClient.invalidateQueries({ queryKey: ['usage'] });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ProjectInput) => createProject(input),
    onSuccess: () => invalidateProjectViews(queryClient),
  });
}

export function useUpdateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: Partial<ProjectInput> }) =>
      updateProject(id, input),
    onSuccess: () => invalidateProjectViews(queryClient),
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteProject(id),
    onSuccess: () => invalidateProjectViews(queryClient),
  });
}
