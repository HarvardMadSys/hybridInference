"""Adapter implementations for specific serving engines."""

import time
from typing import Any

from routing.outsourcing.queue import WaitingQueueInterface
from routing.outsourcing.request import OutsourcingRequestInfo, RequestStatus


class SGLangWaitingQueueAdapter(WaitingQueueInterface):
    """Adapter for SGLang's scheduler waiting queue.
    
    This adapter wraps SGLang's internal scheduler to provide a uniform
    interface for the outsourcing engine.
    
    Example usage:
        from sglang import Scheduler
        
        scheduler = Scheduler(...)
        queue_adapter = SGLangWaitingQueueAdapter(scheduler)
        
        # Use with outsourcing engine
        engine = OutsourcingEngine(
            waiting_queue=queue_adapter,
            ...
        )
    """

    def __init__(self, scheduler: Any):
        """Initialize the adapter.
        
        Args:
            scheduler: SGLang scheduler instance with waiting queue access
        """
        self.scheduler = scheduler
        
    def get_all_waiting(self) -> list[OutsourcingRequestInfo]:
        """Get snapshot of all waiting requests in queue order (FCFS).
        
        Returns:
            List of OutsourcingRequestInfo for all waiting requests
        """
        # Access SGLang's waiting queue
        # The actual attribute name may vary depending on SGLang version
        # Common patterns: waiting_queue, waiting_reqs, pending_requests
        waiting_reqs = getattr(self.scheduler, 'waiting_queue', [])
        
        if not waiting_reqs:
            # Try alternative attribute names
            waiting_reqs = getattr(self.scheduler, 'waiting_reqs', [])
        
        if not waiting_reqs:
            waiting_reqs = getattr(self.scheduler, 'pending_requests', [])
        
        result = []
        current_time = time.time()
        
        for req in waiting_reqs:
            try:
                # Extract request information from SGLang request object
                request_id = getattr(req, 'request_id', getattr(req, 'rid', str(id(req))))
                
                # Timing information
                arrival_time = getattr(req, 'created_time', getattr(req, 'arrival_time', current_time))
                queue_time = current_time - arrival_time
                
                # Token information
                # SGLang typically stores tokens or token IDs
                prompt_tokens = getattr(req, 'prompt_tokens', getattr(req, 'input_ids', []))
                num_prompt_tokens = len(prompt_tokens) if hasattr(prompt_tokens, '__len__') else 0
                
                # Output tokens - check sampling params
                sampling_params = getattr(req, 'sampling_params', None)
                if sampling_params:
                    num_output_tokens = getattr(
                        sampling_params, 
                        'max_new_tokens',
                        getattr(sampling_params, 'max_tokens', 512)
                    )
                else:
                    num_output_tokens = 512  # Default fallback
                
                # Processed tokens
                num_processed_tokens = getattr(req, 'num_processed_tokens', 0)
                
                # SLO information (if available in metadata)
                metadata = getattr(req, 'metadata', {}) or {}
                prefill_slo = metadata.get('prefill_slo_seconds')
                total_slo = metadata.get('total_slo_seconds')
                
                # Status and prefill completion
                is_prefill_complete = getattr(req, 'is_prefill_complete', False)
                
                # Pricing (can be customized per request via metadata)
                input_price = metadata.get('input_price_per_token', 1.25 / 1_000_000)
                output_price = metadata.get('output_price_per_token', 10.0 / 1_000_000)
                
                result.append(
                    OutsourcingRequestInfo(
                        request_id=request_id,
                        arrival_time=arrival_time,
                        queue_time=queue_time,
                        num_prompt_tokens=num_prompt_tokens,
                        num_output_tokens=num_output_tokens,
                        num_processed_tokens=num_processed_tokens,
                        prefill_slo_seconds=prefill_slo,
                        total_slo_seconds=total_slo,
                        status=RequestStatus.WAITING,
                        is_prefill_complete=is_prefill_complete,
                        input_price_per_token=input_price,
                        output_price_per_token=output_price,
                        metadata=metadata,
                    )
                )
            except Exception as e:
                # Log error but continue processing other requests
                print(f"Warning: Failed to convert request {getattr(req, 'request_id', 'unknown')}: {e}")
                continue
        
        return result
    
    def remove_requests(self, request_ids: set[str]) -> list[OutsourcingRequestInfo]:
        """Remove specified requests from the waiting queue.
        
        Args:
            request_ids: Set of request IDs to remove
            
        Returns:
            List of removed OutsourcingRequestInfo objects
        """
        # Get current waiting requests before removal
        all_waiting = self.get_all_waiting()
        to_remove = [req for req in all_waiting if req.request_id in request_ids]
        
        # Access SGLang's waiting queue
        waiting_attr_name = None
        for attr in ['waiting_queue', 'waiting_reqs', 'pending_requests']:
            if hasattr(self.scheduler, attr):
                waiting_attr_name = attr
                break
        
        if waiting_attr_name is None:
            print("Warning: Could not find waiting queue attribute in scheduler")
            return []
        
        # Filter out the requests to remove
        current_queue = getattr(self.scheduler, waiting_attr_name)
        
        # Create a set for faster lookup
        ids_to_remove = set(request_ids)
        
        # Filter the queue
        filtered_queue = []
        for req in current_queue:
            req_id = getattr(req, 'request_id', getattr(req, 'rid', str(id(req))))
            if req_id not in ids_to_remove:
                filtered_queue.append(req)
        
        # Update the scheduler's queue
        setattr(self.scheduler, waiting_attr_name, filtered_queue)
        
        return to_remove
    
    def get_length(self) -> int:
        """Current number of waiting requests.
        
        Returns:
            Number of requests in the waiting queue
        """
        # Try different attribute names
        for attr in ['waiting_queue', 'waiting_reqs', 'pending_requests']:
            queue = getattr(self.scheduler, attr, None)
            if queue is not None:
                return len(queue)
        
        return 0
    
    def peek(self) -> OutsourcingRequestInfo | None:
        """Look at the head of the queue without removing.
        
        Returns:
            OutsourcingRequestInfo for the first request, or None if empty
        """
        waiting = self.get_all_waiting()
        return waiting[0] if waiting else None
