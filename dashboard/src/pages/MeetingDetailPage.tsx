import { useMemo, useRef, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Separator } from '@/components/ui/separator';
import { LiveIndicator } from '@/components/shared/LiveIndicator';
import { ChatMessage } from '@/components/meetings/ChatMessage';
import { useMeeting, useMeetingMessages } from '@/api/meetings';
import { ArrowLeft, MessageSquare, Users } from 'lucide-react';
import type { MeetingMessage } from '@/types';

interface MessageGroup {
  round: number;
  messages: MeetingMessage[];
}

export function MeetingDetailPage() {
  const { meetingId } = useParams<{ meetingId: string }>();
  const {
    data: meetingData,
    isLoading: meetingLoading,
    error: meetingError,
  } = useMeeting(meetingId ?? '');
  const { data: messagesData, isLoading: messagesLoading } =
    useMeetingMessages(meetingId ?? '');

  const meeting = meetingData?.data;
  const messages = messagesData?.data ?? [];

  // Group messages by round_number
  const grouped = useMemo<MessageGroup[]>(() => {
    const map = new Map<number, MeetingMessage[]>();
    for (const msg of messages) {
      const round = msg.round_number;
      if (!map.has(round)) map.set(round, []);
      map.get(round)!.push(msg);
    }
    return Array.from(map.entries())
      .sort(([a], [b]) => a - b)
      .map(([round, msgs]) => ({
        round,
        messages: msgs.sort(
          (a, b) =>
            new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime(),
        ),
      }));
  }, [messages]);

  // Auto-scroll to bottom
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const isActive = meeting?.status === 'active';

  if (meetingLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-48" />
        <Card>
          <CardContent className="pt-6">
            <div className="space-y-3">
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-2/3" />
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (meetingError || !meeting) {
    return (
      <div className="space-y-6">
        <Button variant="ghost" render={<Link to="/meetings" />}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          返回会议列表
        </Button>
        <div className="py-12 text-center">
          <p className="text-sm text-destructive">
            {meetingError
              ? `加载失败: ${meetingError.message}`
              : '会议不存在'}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col space-y-4">
      {/* Top bar */}
      <div className="flex items-center gap-3">
        <Button
          variant="ghost"
          size="sm"
          className="-ml-2"
          render={<Link to="/meetings" />}
        >
          <ArrowLeft className="mr-1 h-4 w-4" />
          返回
        </Button>
        <Separator orientation="vertical" className="h-5" />
        <div className="flex items-center gap-2">
          <MessageSquare className="h-4 w-4 text-muted-foreground" />
          <h1 className="font-semibold">{meeting.topic}</h1>
        </div>
        <div className="flex items-center gap-2">
          {isActive ? (
            <div className="flex items-center gap-1.5">
              <LiveIndicator />
              <Badge className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400">
                进行中
              </Badge>
            </div>
          ) : (
            <Badge variant="secondary">会议已结束</Badge>
          )}
        </div>
        <div className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
          <Users className="h-3 w-3" />
          <span>{meeting.participants.length} 人参与</span>
        </div>
      </div>

      {/* Message stream */}
      <Card className="flex-1 overflow-hidden">
        <CardHeader className="py-3">
          <CardTitle className="text-sm">会议讨论</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <div ref={scrollRef} className="max-h-[calc(100vh-260px)] overflow-y-auto px-4 pb-4">
            {messagesLoading ? (
              <div className="space-y-3 p-4">
                {Array.from({ length: 4 }).map((_, i) => (
                  <div key={i} className="flex gap-3">
                    <Skeleton className="h-8 w-8 rounded-full" />
                    <div className="flex-1 space-y-2">
                      <Skeleton className="h-3 w-24" />
                      <Skeleton className="h-4 w-full" />
                    </div>
                  </div>
                ))}
              </div>
            ) : messages.length === 0 ? (
              <div className="py-12 text-center">
                <p className="text-sm text-muted-foreground">暂无讨论消息</p>
              </div>
            ) : (
              <div className="space-y-1">
                {grouped.map((group) => (
                  <div key={group.round}>
                    {/* Round divider */}
                    <div className="flex items-center gap-3 py-3">
                      <Separator className="flex-1" />
                      <span className="shrink-0 text-xs font-medium text-muted-foreground">
                        第 {group.round} 轮
                      </span>
                      <Separator className="flex-1" />
                    </div>
                    {/* Messages */}
                    {group.messages.map((msg) => (
                      <ChatMessage key={msg.id} message={msg} />
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
